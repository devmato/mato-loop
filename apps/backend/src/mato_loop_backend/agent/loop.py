from __future__ import annotations

import asyncio
import secrets
import time
from functools import partial
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import mato_loop_backend.agent.run_support as agent_run_support
from mato_loop_backend.agent.context import load_missing_attachments
from mato_loop_backend.agent.media_model_helpers import media_model_handlers
from mato_loop_backend.agent.memory_context import MemoryCache, prepare_memory_context
from mato_loop_backend.agent.model_turn import complete_model_turn, persist_model_response
from mato_loop_backend.agent.pdf_attachments import prepare_pdf_attachments
from mato_loop_backend.agent.project_worktrees import prepare_project_worktrees
from mato_loop_backend.agent.projection import ContextError
from mato_loop_backend.agent.prompts import (
    agent_profile_runtime_instructions,
    general_system_prompt,
    tool_budget_prompt,
)
from mato_loop_backend.agent.repository_instructions import repository_instructions_prompt
from mato_loop_backend.agent.run_contracts import AgentRunError, _VisibleResponse
from mato_loop_backend.agent.run_support import (
    _OPENSCAD_FENCE_PATTERN,
    _OUTPUT_LIMIT_REASONS,
    EMPTY_RESPONSE_RETRIES,
    REQUIRED_MEMORY_WRITE_RETRIES,
    REQUIRED_OPENSCAD_RENDER_RETRIES,
    REQUIRED_OPENSCAD_SOURCE_REVIEW_RETRIES,
    REQUIRED_WEB_SEARCH_RETRIES,
    TRUNCATED_TOOL_BATCH_RETRIES,
    logger,
)
from mato_loop_backend.agent.run_timing import run_timing_payload, transition_run_status
from mato_loop_backend.agent.script_tools import SCRIPT_TOOL
from mato_loop_backend.agent.session_context import prepare_session_contexts
from mato_loop_backend.agent.session_secrets import session_secrets
from mato_loop_backend.agent.session_settings import apply_run_session_settings
from mato_loop_backend.agent.subagents import (
    apply_delegation_ceiling,
    delegation_snapshot,
    deliver_updates,
    has_children,
    integration_ceiling_ids,
    wait_for_children,
)
from mato_loop_backend.agent.tool_round import execute_tool_batch
from mato_loop_backend.agent.turn_context import build_turn_context
from mato_loop_backend.agent.turn_tools import select_turn_tools
from mato_loop_backend.auth.credentials import CredentialCipher
from mato_loop_backend.automations import AutomationService
from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.diagnostics.service import schedule_diagnostic_event
from mato_loop_backend.events import run_events
from mato_loop_backend.execution import execution_agent_connections
from mato_loop_backend.files.service import RootFileService
from mato_loop_backend.integrations.search import search_available
from mato_loop_backend.media.attachments import AttachmentService
from mato_loop_backend.memory import policy as memory_policy
from mato_loop_backend.message_content import canonical_assistant_content
from mato_loop_backend.models import (
    AgentRun,
    ChatSession,
    ContextSummary,
    ExecutionAgent,
    Message,
    Project,
    WebSearchPolicy,
)
from mato_loop_backend.privacy import shared_protection_cache
from mato_loop_backend.providers.base import ProviderImageInput, ProviderTurnState, ToolCall
from mato_loop_backend.providers.reasoning import reasoning_profile
from mato_loop_backend.sessions.state import is_session_deleted
from mato_loop_backend.settings import (
    AgentProfileUnavailable,
    AgentRuntimeSettings,
    InstanceLimits,
    MemorySettings,
    SettingsStore,
    UserProfileSettings,
    apply_session_ai,
    effective_model_pseudonymization,
    effective_model_trust,
    load_agent_profile,
    load_agent_profile_tools,
    load_effective_agent_identity,
    load_effective_ai,
    load_effective_integrations,
)
from mato_loop_backend.settings.effective import filter_agent_profile_integrations
from mato_loop_backend.settings.language import preferred_language
from mato_loop_backend.settings.models import ReasoningEffort, TrustLevel
from mato_loop_backend.skills import RunSkillState, SkillService
from mato_loop_backend.tools.discovery import expire_discoveries
from mato_loop_backend.tools.display import tool_display
from mato_loop_backend.tools.secrets import available_secret_references
from mato_loop_backend.tools.snapshots import collect_tool_snapshots
from mato_loop_backend.user_profile import user_profile_content_hashes, user_profile_runtime_payload
from mato_loop_backend.web.access import webfetch_available


async def execute_loop_body(
    cls,
    db: AsyncSession,
    run: AgentRun,
    visible_response: _VisibleResponse | None,
    ephemeral_images: dict[tuple[UUID, int, str, int], ProviderImageInput],
    one_time_secrets: dict[UUID, str],
) -> None:
    run_secret_refs: dict[str, str] = {}
    visible_response = visible_response or _VisibleResponse()
    chat_session = await db.get(ChatSession, run.session_id)
    if chat_session is None:
        raise AgentRunError("session.notFound")
    apply_run_session_settings(chat_session, run)
    diagnostics_active = getattr(run, "_diagnostics_active", False) is True
    delegated = chat_session.parent_session_id is not None
    ceiling = await delegation_snapshot(db, run, chat_session)
    project = (
        await db.get(Project, chat_session.project_id)
        if chat_session.project_id is not None
        else None
    )
    project_name = project.name if project is not None else None
    project_instructions = project.instructions if project is not None else ""
    await cls._ensure_user_active(db, chat_session.user_id)
    ai_settings = await load_effective_ai(db, chat_session.user_id)
    runtime_settings = await SettingsStore.load(db, AgentRuntimeSettings)
    instance_limits = await SettingsStore.load(db, InstanceLimits)
    integration_settings = await load_effective_integrations(
        db, chat_session.user_id
    )
    user_profile = await SettingsStore.load(
        db, UserProfileSettings, user_id=chat_session.user_id
    )
    response_language = await preferred_language(db, chat_session.user_id) if not delegated else None
    skills = await SkillService.list(db, chat_session.user_id)
    skill_state = RunSkillState.from_skills(skills)
    selected_execution_agent = (
        await db.get(ExecutionAgent, run.execution_agent_id)
        if run.execution_agent_id is not None
        else None
    )
    if (
        not isinstance(selected_execution_agent, ExecutionAgent)
        or selected_execution_agent.user_id != chat_session.user_id
        or selected_execution_agent.revoked_at is not None
    ):
        selected_execution_agent = None
    agent_profile_id = run.agent_profile_id or chat_session.agent_profile_id
    from mato_loop_backend.settings.effective import session_agent_allowed

    if not session_agent_allowed(chat_session, agent_profile_id):
        raise AgentRunError("agentProfile.selectedUnavailable")
    try:
        prompt_agent_profile = await load_agent_profile(
            db, chat_session.user_id, agent_profile_id
        )
    except AgentProfileUnavailable as exc:
        raise AgentRunError("agentProfile.selectedUnavailable") from exc
    ai_settings = apply_session_ai(ai_settings, chat_session, agent_profile_id)
    model_id = run.model_config_id or ai_settings.default_model_id
    model = agent_run_support._model_for_id(ai_settings, model_id)
    if model is None:
        raise AgentRunError("session.modelUnavailable")
    if not model.supports_tools:
        raise AgentRunError("session.modelToolsUnavailable")
    provider = agent_run_support._provider_for_model(ai_settings, model)
    if provider is None:
        raise AgentRunError("provider.notFound")
    profile_instructions = agent_profile_runtime_instructions(
        prompt_agent_profile, model_id=model.model_id, delegated=delegated,
    )
    model_reasoning_profile = reasoning_profile(
        model.model_id, model.discovery_metadata
    )
    can_disable_reasoning = (
        model_reasoning_profile is not None
        and ReasoningEffort.NONE in model_reasoning_profile.options
    )
    model_trust_level = TrustLevel(
        run.trust_level or effective_model_trust(provider, model).value
    )
    agent_identity = await load_effective_agent_identity(
        db, chat_session.user_id, prompt_agent_profile
    )
    input_message = await db.get(Message, run.input_message_id) if run.input_message_id else None
    if not isinstance(input_message, Message):
        input_message = next((item for item in reversed(await cls._records(db, chat_session.id)) if item.role == "user"), None)
    input_metadata = (input_message.provider_data or {}).get("_matoloop", {}) if input_message else {}
    budgets = input_metadata.get("maxToolCallsByRun", {})
    tool_call_limit = budgets.get(
        str(run.id), input_metadata.get(
            "maxToolCalls", prompt_agent_profile.max_tool_calls if prompt_agent_profile is not None and not delegated else 25
        )
    )
    if tool_call_limit is not None and (type(tool_call_limit) is not int or tool_call_limit < 1):
        raise AgentRunError("agent.toolCallLimitReached")
    if input_message is not None and str(run.id) not in budgets:
        agent_run_support._snapshot_tool_call_limit(input_message, run.id, tool_call_limit)
        run.input_message_id = input_message.id
        await db.commit()
    integration_settings = filter_agent_profile_integrations(
        integration_settings, prompt_agent_profile, allowed_ids=integration_ceiling_ids(ceiling)
    )
    user_profile_payload = user_profile_runtime_payload(
        user_profile, model_trust_level
    )
    excluded_memory_content_hashes = user_profile_content_hashes(user_profile)
    run_pseudonymization_enabled = (
        run.pseudonymization_enabled
        if run.pseudonymization_enabled is not None
        else effective_model_pseudonymization(provider, model)
    )

    memory_state: memory_policy.EffectiveMemory | None = None

    effective_search = search_available(integration_settings)
    required_search = not delegated and chat_session.web_search_policy == WebSearchPolicy.REQUIRED
    if required_search and not effective_search:
        raise AgentRunError("integration.searchUnavailable")
    if required_search and not model.supports_tools:
        raise AgentRunError("agent.requiredWebSearchUnsupported")
    model_requests = 0

    def count_helper_request() -> None:
        nonlocal model_requests
        model_requests += 1

    def next_helper_request() -> int:
        return model_requests + 1
    required_search_completed = False
    required_search_retries = 0
    empty_response_retries = 0
    openscad_render_pending = False
    openscad_render_retries = 0
    openscad_pending_paths: set[tuple[str, str]] = set()
    openscad_source_review_pending: tuple[str, str] | None = None
    openscad_source_review_retries = 0
    memory_write_pending = False
    memory_write_retries = 0
    memory_write_outcome: str | None = None
    research_sources: dict[str, str] = {}
    research_source_titles: dict[str, str] = {}
    research_used = False
    research_tool_calls_used = 0
    successful_research_calls: dict[str, dict[str, Any]] = {}
    tool_calls_used = 0
    recorded_tool_calls = 0
    failed_tool_call_signatures: dict[str, str] = {}
    retried_transient_reads: set[str] = set()
    repeated_tool_call_repair_available = True
    pending_tool_repairs: dict[str, Message] = {}
    budget_finalization = False
    script_finalization = False
    read_evidence_available = False
    empty_script_rounds = 0
    interrupted_script_edits: dict[str, UUID] = {}
    truncated_tool_batches = 0
    live_tool_calls: dict[UUID, list[dict[str, Any]]] = {}
    pseudonymization_key = (
        CredentialCipher().derive_key(
            b"matoloop:privacy-session:v1\0" + chat_session.user_id.bytes + chat_session.id.bytes
        ) if run_pseudonymization_enabled else secrets.token_bytes(32)
    )
    pseudonymization_cache = shared_protection_cache()
    session_read_cursors: dict[UUID, UUID | None] = {}
    memory_cache = MemoryCache()
    pdf_cache = {}
    continuation_context_stale = False
    previous_tool_names: tuple[str, ...] = ()
    provider_turn_state = ProviderTurnState()
    provider_turn_message_id = None

    while True:
        continuation_received = await cls._await_continuations(run.id)
        if continuation_received:
            if run.status == "waiting_input":
                transition_run_status(run, "running")
                await db.commit()
                await run_events.publish(run.id, {"type": "run.timing", "runId": str(run.id), **run_timing_payload(run)})
            await cls._transcribe_continuation_messages(db, run)
        if continuation_received or continuation_context_stale:
            empty_script_rounds = 0
            script_finalization = False
            read_evidence_available = False
            interrupted_script_edits.clear()
            for signature, code in tuple(failed_tool_call_signatures.items()):
                if code == "tool.scriptEditAlreadyApplied":
                    failed_tool_call_signatures.pop(signature)
            if await cls._apply_continuation_profile(db, run):
                agent_profile_id = run.agent_profile_id
                prompt_agent_profile = await load_agent_profile(db, chat_session.user_id, agent_profile_id)
                ai_settings = apply_session_ai(await load_effective_ai(db, chat_session.user_id), chat_session, agent_profile_id)
                model = agent_run_support._model_for_id(ai_settings, run.model_config_id)
                provider = agent_run_support._provider_for_model(ai_settings, model)
                if model_trust_level != TrustLevel(run.trust_level):
                    live_tool_calls.clear()
                model_trust_level = TrustLevel(run.trust_level)
                run_pseudonymization_enabled = run.pseudonymization_enabled
                profile_instructions = agent_profile_runtime_instructions(
                    prompt_agent_profile, model_id=model.model_id, delegated=delegated,
                )
                model_reasoning_profile = reasoning_profile(model.model_id, model.discovery_metadata)
                can_disable_reasoning = model_reasoning_profile is not None and ReasoningEffort.NONE in model_reasoning_profile.options
                agent_identity = await load_effective_agent_identity(db, chat_session.user_id, prompt_agent_profile)
                user_profile_payload = user_profile_runtime_payload(user_profile, model_trust_level)
                pseudonymization_key = CredentialCipher().derive_key(
                    b"matoloop:privacy-session:v1\0" + chat_session.user_id.bytes + chat_session.id.bytes
                ) if run_pseudonymization_enabled else secrets.token_bytes(32)
                tool_call_limit = prompt_agent_profile.max_tool_calls
                budget_finalization = tool_call_limit is not None and tool_calls_used >= tool_call_limit
                provider_turn_state = ProviderTurnState()
                pdf_cache.clear()
            selected_execution_agent = None
            memory_cache.invalidate(continuation=True)
            if project is not None:
                await db.refresh(project)
            continuation_context_stale = False
        await deliver_updates(db, run, chat_session)
        tool_budget_exhausted = script_finalization or budget_finalization or (
            tool_call_limit is not None and tool_calls_used >= tool_call_limit
        )
        if tool_budget_exhausted:
            memory_write_pending = False
            openscad_render_pending = False
            openscad_source_review_pending = None
        await cls._ensure_user_active(db, chat_session.user_id)
        context_started_at = time.monotonic()
        await agent_run_support._publish_run_phase(
            run.id,
            "preparing_context",
            modelRequest=model_requests + 1,
        )
        await db.refresh(chat_session)
        apply_run_session_settings(chat_session, run)
        if run.execution_agent_id != (
            selected_execution_agent.id if selected_execution_agent is not None else None
        ):
            selected_execution_agent = (
                await db.get(ExecutionAgent, run.execution_agent_id)
                if run.execution_agent_id is not None else None
            )
            if not isinstance(selected_execution_agent, ExecutionAgent) or (
                selected_execution_agent.user_id != chat_session.user_id
                or selected_execution_agent.revoked_at is not None
            ):
                selected_execution_agent = None
        ai_settings = apply_session_ai(
            await load_effective_ai(db, chat_session.user_id),
            chat_session, agent_profile_id,
        )
        runtime_settings = await SettingsStore.load(db, AgentRuntimeSettings)
        instance_limits = await SettingsStore.load(db, InstanceLimits)
        memory_settings = await SettingsStore.load(
            db, MemorySettings, user_id=chat_session.user_id
        )
        integration_settings = await load_effective_integrations(
            db, chat_session.user_id
        )
        try:
            tool_settings, _active_agent_profile = await load_agent_profile_tools(
                db,
                chat_session.user_id,
                agent_profile_id,
                model_trust_level,
                integrations=integration_settings,
            )
        except AgentProfileUnavailable as exc:
            raise AgentRunError("agentProfile.selectedUnavailable") from exc
        tool_settings = apply_delegation_ceiling(tool_settings, ceiling)
        integration_settings = filter_agent_profile_integrations(
            integration_settings, _active_agent_profile, allowed_ids=integration_ceiling_ids(ceiling)
        )
        web_fetch_enabled = webfetch_available(tool_settings)
        memory_state = memory_policy.effective_memory(
            chat_session, memory_settings, provider, model
        )
        if delegated:
            memory_state = memory_policy.EffectiveMemory(False, False, False)
        records = await cls._records(db, chat_session.id)
        review_context = ""
        from mato_loop_backend.agent.interaction_context import interaction_context, run_records
        from mato_loop_backend.agent.session_execution import has_other_runs

        if chat_session.parent_session_id is None:
            records = run_records(records, run)
        context_timings = {"settingsAndHistoryMs": round((time.monotonic() - context_started_at) * 1000)}
        current_user = next((item for item in reversed(records) if item.role == "user"), None)
        if current_user is not None and current_user.id != provider_turn_message_id:
            provider_turn_state = ProviderTurnState()
            provider_turn_message_id = current_user.id
        if current_user is not None:
            data = current_user.provider_data or {}
            metadata = data.get("_matoloop", {})
            updated = {**metadata, "agentProfileInstructions": profile_instructions}
            # A retry reuses the user message but may select another workspace.
            if metadata.get("workspaceContextRunId") != str(run.id):
                workspace = {"executionAgentId": str(run.execution_agent_id) if run.execution_agent_id else None,
                             "currentDirectory": run.current_directory_snapshot}
                previous_user = next((item for item in reversed(records)
                                      if item.role == "user" and item.id != current_user.id), None)
                previous_workspace = ((previous_user.provider_data or {}).get("_matoloop", {}).get("workspaceContext")
                                      if previous_user is not None else None)
                if previous_user is not None and previous_workspace is None:
                    # Read the legacy baseline once; subsequent turns use their saved workspace context.
                    previous_run = await db.scalar(select(AgentRun).where(
                        AgentRun.session_id == run.session_id, AgentRun.id != run.id,
                        AgentRun.input_message_id != current_user.id, AgentRun.parent_run_id.is_(None),
                    ).order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(1))
                    if isinstance(previous_run, AgentRun):
                        previous_workspace = {
                            "executionAgentId": str(previous_run.execution_agent_id) if previous_run.execution_agent_id else None,
                            "currentDirectory": previous_run.current_directory_snapshot,
                        }
                updated["workspaceContext"] = workspace
                updated["workspaceContextRunId"] = str(run.id)
                updated["workspaceChanged"] = previous_workspace is not None and previous_workspace != workspace
            if updated != metadata:
                current_user.provider_data = {
                    **data, "_matoloop": updated,
                }
                await db.commit()
        summary = None
        if any((item.provider_data or {}).get("_matoloop", {}).get("toolDiscovery") for item in records):
            summary = await db.scalar(select(ContextSummary).where(ContextSummary.session_id == chat_session.id)
                                      .order_by(ContextSummary.created_at.desc(), ContextSummary.id.desc()).limit(1))
        summary_boundary = next((index + 1 for index, item in enumerate(records)
                                 if summary is not None and item.id == summary.covered_through_message_id), 0)
        root_files = RootFileService(
            chat_session.id,
            run_id=run.id,
            project_id=chat_session.project_id,
            user_id=chat_session.user_id,
            user_quota_bytes=instance_limits.user_quota_bytes,
            max_editable_text_bytes=instance_limits.max_editable_text_bytes,
        )
        session_context_authorize = partial(
            cls._authorize, db, run, chat_session,
            ToolCall(id=f"session-context:{run.id}", name="session_state", arguments={"operation": "read"}),
            root_files=root_files, check_only=True,
        )
        session_contexts = await prepare_session_contexts(
            db, chat_session, records, run_id=run.id, profile=_active_agent_profile,
            trust_level=model_trust_level, authorize=session_context_authorize,
        )
        async def authorize_project_metadata(request, root_files=root_files):
            return await cls._authorize(
                db, run, chat_session,
                ToolCall(id=f"project-worktrees:{run.id}",
                         name="read" if request.capability == "file.read" else "context_search", arguments={}),
                request, root_files=root_files, check_only=True,
            )

        for message_id, worktrees in (await prepare_project_worktrees(
            db, chat_session, project, records, run_id=run.id,
            trust_level=model_trust_level, authorize=authorize_project_metadata,
        )).items():
            session_contexts[message_id] = {"session": session_contexts.get(message_id, ""), "worktrees": worktrees}
        if chat_session.parent_session_id is None and has_other_runs(run.id, chat_session.id):
            records, review_context = await interaction_context(db, chat_session, run, records)
        memory_started = time.monotonic()
        memory_turn = await prepare_memory_context(
            db, chat_session, records,
            model=model,
            trust_level=model_trust_level,
            memory_state=memory_state,
            settings=memory_settings,
            runtime_settings=runtime_settings,
            excluded_content_hashes=excluded_memory_content_hashes,
            embedding_enabled=lambda: get_runtime_config().embedding_enabled,
            cache=memory_cache,
            known_names=(
                *((project.name,) if project is not None else ()),
                *(item.name for item in integration_settings.instances if item.enabled),
            ),
        )
        memory_overview_prefetched = memory_turn.overview_prefetched
        context_timings["memoryMs"] = round((time.monotonic() - memory_started) * 1000)
        memory_overview_runtime_state = memory_turn.overview_runtime_state
        tools_started = time.monotonic()
        turn_tools = select_turn_tools(
            run, chat_session,
            model=model, ai_settings=ai_settings, tool_settings=tool_settings,
            integration_settings=integration_settings, project=project,
            identity=agent_identity, skill_state=skill_state,
            active_profile=_active_agent_profile, ceiling=ceiling,
            delegated=delegated, memory_read_enabled=memory_state.read_enabled,
            memory_write_enabled=memory_state.write_enabled,
            web_fetch_enabled=web_fetch_enabled,
            required_search=required_search,
            required_search_completed=required_search_completed,
            memory_write_pending=memory_write_pending,
            source_review_pending=openscad_source_review_pending,
            render_pending=openscad_render_pending,
            tool_budget_exhausted=tool_budget_exhausted,
            remaining_tool_calls=(None if tool_call_limit is None else max(0, tool_call_limit - tool_calls_used)),
            discovery_records=records[summary_boundary:],
            previous_tool_names=previous_tool_names,
        )
        integration_settings = turn_tools.integration_settings
        execution_client = turn_tools.execution_client
        effective_toolset = turn_tools.toolset
        schemas = turn_tools.schemas
        previous_tool_names = tuple(effective_toolset.tools) if effective_toolset else ()
        offered_tool_names = turn_tools.offered_names
        effective_tool_count = turn_tools.effective_count
        tool_schema_tokens = turn_tools.schema_tokens
        inactive_capability_groups = turn_tools.inactive_groups
        search_pending = turn_tools.search_pending
        context_timings["toolSelectionMs"] = round((time.monotonic() - tools_started) * 1000)
        repository_context_loader = None
        repository_entries = None
        repository_authorize = None
        if (_active_agent_profile is not None and _active_agent_profile.repository_instructions_enabled
                and tool_settings.tool_availability.get("read", True)
                and selected_execution_agent is not None and execution_client is not None
                and execution_client.supports("filesystem.stat") and execution_client.supports("filesystem.read")):
            repository_entries = {}
            repository_authorize = partial(
                cls._authorize, db, run, chat_session,
                ToolCall(id=f"repository-instructions:{run.id}", name="read",
                         arguments={"root": "computer", "path": "AGENTS.md"}),
                root_files=root_files, check_only=True,
            )
            repository_context_loader = partial(
                repository_instructions_prompt,
                execution_client, records, repository_authorize,
                checked_entries=repository_entries,
                discovery_scope=(chat_session.user_id, chat_session.id),
            )
        workspace = chat_session.coding_workspace or {}
        assigned_worktree = None
        if (
            not delegated
            and selected_execution_agent is not None
            and workspace.get("mode") == "worktree"
            and workspace.get("executionAgentId") == str(selected_execution_agent.id)
        ):
            from mato_loop_backend.sessions.service import execution_filesystem_policy

            path = workspace.get("path")
            if isinstance(path, str) and path and execution_filesystem_policy(selected_execution_agent, path) != "deny":
                assigned_worktree = path
        render_system_prompt = partial(
            general_system_prompt,
            chat_session,
            model_trust_level=model_trust_level,
            delegated=delegated,
            agent_identity=agent_identity,
            persona=agent_identity.persona,
            agent_profile=prompt_agent_profile,
            user_profile=user_profile_payload,
            response_language=response_language,
            project_name=project_name,
            project_instructions=project_instructions,
            project_execution_agent_id=project.default_execution_agent_id if project is not None else None,
            project_directory=project.default_current_directory if project is not None else None,
            memory_context_enabled=memory_state.read_enabled,
            memory_overview_prefetched=memory_overview_prefetched,
            web_search_required=required_search and not tool_budget_exhausted,
            web_search_completed=required_search_completed,
            web_search_retry=required_search_retries,
            response_repair_required=empty_response_retries > 0,
            openscad_render_pending=openscad_render_pending,
            openscad_source_review_pending=openscad_source_review_pending,
            memory_write_pending=memory_write_pending,
            memory_write_outcome=memory_write_outcome,
            tool_call_limit=tool_call_limit,
            research_tool_calls_used=research_tool_calls_used,
            research_tool_call_limit=None,
            include_local_time=False,
            offered_tool_names=frozenset(
                effective_toolset.tools if effective_toolset else ()
            ),
            tool_instructions=(
                effective_toolset.prompt_fragments if effective_toolset else ()
            ),
            inactive_capability_groups=inactive_capability_groups,
            skill_catalog=(
                tuple(skill_state.catalog.values())
                if effective_toolset is not None
                and {"load_skill", "edit_skill"} & effective_toolset.tools.keys()
                else ()
            ),
            active_skills=tuple(skill_state.active.values()),
            execution_agent_id=run.execution_agent_id,
            execution_agent_name=(
                selected_execution_agent.display_name if selected_execution_agent else None
            ),
            execution_agent_platform=(
                selected_execution_agent.platform if selected_execution_agent else None
            ),
            execution_agent_connected=(
                execution_agent_connections.is_connected(selected_execution_agent.id)
                if selected_execution_agent is not None
                else False
            ),
            execution_agent_system_info=(
                execution_agent_connections.system_info(selected_execution_agent.id)
                if selected_execution_agent is not None
                else None
            ),
            temporary_files_enabled=tool_settings._temporary_files_enabled,
            current_directory=run.current_directory_snapshot,
            assigned_worktree=assigned_worktree,
            session_environment={
                **{name: {"valueRef": ref} for name, ref in run_secret_refs.items()},
                **session_secrets.prompt_entries(chat_session.id),
            },
        )

        system_prompt = render_system_prompt()
        finalization_context = (
            "Tool execution stopped after repeated failed calls. Answer using the evidence already collected. "
            "State what you could not verify. Do not invent results."
            if script_finalization else None
        )
        request_options: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "tools": schemas,
            "tool_choice": "none" if script_finalization else (
                "required" if not tool_budget_exhausted and (
                    search_pending
                    or memory_write_pending
                    or openscad_render_pending
                    or openscad_source_review_pending is not None
                )
                else "auto"
            ),
            "timeout": runtime_settings.request_timeout_seconds,
            "target_trust_level": model_trust_level,
            "pseudonymization_enabled": run_pseudonymization_enabled,
            "pseudonymization_key": pseudonymization_key,
            "pseudonymization_cache": pseudonymization_cache,
            "prompt_cache_key": str(chat_session.id),
            "required_image_ids": (),
            "reasoning_effort": (
                ReasoningEffort.NONE
                if empty_response_retries > 0 and can_disable_reasoning
                else ai_settings.reasoning_effort
            ),
        }

        pdf_handler, *_ = media_model_handlers(
            db=db, run=run, session=chat_session, ai_settings=ai_settings,
            model=model, model_trust_level=model_trust_level,
            runtime_settings=runtime_settings, tool_settings=tool_settings,
            tool_phase={}, next_model_request=next_helper_request,
            count_model_request=count_helper_request,
        )
        pdf_started = time.monotonic()
        await load_missing_attachments(db, records)
        pdf_context = await prepare_pdf_attachments(
            records, AttachmentService(settings=instance_limits, run_id=run.id),
            images=ephemeral_images, cache=pdf_cache,
            supports_images=model.supports_image_input,
            trust=model_trust_level, handler=pdf_handler,
        )
        context_timings["pdfMs"] = round((time.monotonic() - pdf_started) * 1000)
        try:
            projection, injected_memory_count, injected_episode_count = await build_turn_context(
                cls, db, run, chat_session,
                ai_settings=ai_settings,
                model=model,
                trust_level=model_trust_level,
                settings=runtime_settings,
                instance_limits=instance_limits,
                records=records,
                system_prompt=system_prompt,
                pdf_context=pdf_context,
                runtime_instructions=profile_instructions,
                session_contexts=session_contexts,
                runtime_context="\n\n".join(part for part in (review_context, tool_budget_prompt(
                    None if tool_call_limit is None else max(0, tool_call_limit - tool_calls_used),
                ), finalization_context) if part) or None,
                schemas=schemas,
                offered_tool_names=frozenset(effective_toolset.tools if effective_toolset else ()),
                tool_snapshots=collect_tool_snapshots(
                    records, effective_toolset.tools if effective_toolset else {},
                    tool_settings, chat_session.id,
                ),
                memory_turn=memory_turn,
                memory_read_enabled=memory_state.read_enabled,
                request_options=request_options,
                live_tool_calls=live_tool_calls,
                ephemeral_images=ephemeral_images,
                attachment_service=AttachmentService,
                session_automations=await AutomationService.session_definitions(db, chat_session) if not delegated else None,
                repository_context_loader=repository_context_loader,
                available_secret_references=await available_secret_references(db, chat_session, one_time_secrets),
            )
        except ContextError as exc:
            if exc.code != "agent.contextWindowExceeded" or not expire_discoveries(records[summary_boundary:]):
                raise
            await db.commit()
            continue
        if projection.compacted and projection.context_summary is not None:
            boundary = next((index + 1 for index, item in enumerate(records)
                             if item.id == projection.context_summary.covered_through_message_id), 0)
            if any((item.provider_data or {}).get("_matoloop", {}).get("toolDiscovery")
                   for item in records[summary_boundary:boundary]):
                continue

        hold_response = (
            tool_budget_exhausted
            or search_pending
            or memory_write_pending
            or openscad_render_pending
        )
        model_request = model_requests + 1
        if await is_session_deleted(db, chat_session):
            raise AgentRunError("session.inTrash")
        model_turn = await complete_model_turn(
            cls, db, run, chat_session, visible_response,
            projection=projection,
            ephemeral_images=ephemeral_images,
            model=model,
            trust_level=model_trust_level,
            request_options=request_options,
            settings=runtime_settings,
            schemas=schemas,
            model_request=model_request,
            user_message=next((message for message in reversed(records) if message.role == "user"), None),
            hold_response=hold_response,
            provider_turn_state=provider_turn_state,
        )
        if model_turn is None:
            continuation_context_stale = True
            continue
        result = model_turn.result
        provider_duration_ms = model_turn.duration_ms
        provider_started_at = model_turn.started_at
        provider_timing = model_turn.timing
        streamed_reasoning_state = model_turn.streamed_reasoning
        diagnostics_active = model_turn.diagnostics_active
        model_correlation_id = f"model:{model_requests + 1}"
        model_requests += 1
        normalized_calls = [
            ToolCall(
                id=call.id or f"call-{uuid4()}",
                name=call.name,
                arguments=call.arguments,
            )
            for call in result.tool_calls
        ]
        truncated_batch = bool(normalized_calls) and result.finish_reason in _OUTPUT_LIMIT_REASONS
        tool_batch_exceeds_budget = bool(normalized_calls) and (
            tool_budget_exhausted or (
                tool_call_limit is not None and len(normalized_calls) > max(0, tool_call_limit - tool_calls_used)
            )
        )
        rejected_batch_code = (
            "agent.toolCallLimitReached" if tool_batch_exceeds_budget
            else "agent.outputLimitReached" if truncated_batch else None
        )
        tool_calls_used += len(normalized_calls)
        output_budget = result.provider_data.get("output_budget", {})
        if not isinstance(output_budget, dict):
            output_budget = {}
        first_signals = [
            value for value in provider_timing.values() if isinstance(value, int)
        ]
        performance = {
            "modelRequest": model_requests,
            "toolCallsUsed": tool_calls_used,
            "researchToolCallsUsed": research_tool_calls_used,
            "maxToolCalls": tool_call_limit,
            "effectiveToolCount": effective_tool_count,
            "offeredToolCount": len(schemas),
            "offeredToolNames": [schema["name"] for schema in schemas],
            "toolSchemaTokens": tool_schema_tokens,
            "estimatedInputTokens": projection.estimated_input_tokens,
            "inputTokenSource": projection.input_token_source,
            "inputBudgetTokens": projection.input_budget_tokens,
            "reservedOutputTokens": projection.reserved_output_tokens,
            "safetyReserveTokens": runtime_settings.safety_reserve_tokens,
            **{
                key: output_budget[key]
                for key in (
                    "estimatedInputTokens", "finalEstimatedInputTokens", "guardedInputTokens",
                    "reservedOutputTokens", "allocatedOutputTokens", "transmittedMaxOutputTokens",
                    "safetyReserveTokens", "manualLimit", "providerLimit", "helperLimit",
                    "mode", "estimationPolicy", "mediaInputTokensUnknown",
                )
                if key in output_budget
            },
            "contextPreparationMs": max(
                0, round((provider_started_at - context_started_at) * 1_000)
            ),
            "contextPreparation": {**context_timings, **projection.preparation},
            "providerDurationMs": provider_duration_ms,
            "timeToFirstSignalMs": min(first_signals) if first_signals else None,
            "durableMemoryReferences": injected_memory_count,
            "priorSessionEpisodes": injected_episode_count,
            **provider_timing,
        }
        if diagnostics_active:
            schedule_diagnostic_event(
                chat_session.user_id,
                "model.completed",
                component="provider",
                session_id=chat_session.id,
                run_id=run.id,
                correlation_id=model_correlation_id,
                payload={
                    "finishReason": result.finish_reason,
                    "usage": result.usage,
                    "toolCalls": [
                        {"id": call.id, "name": call.name} for call in normalized_calls
                    ],
                    "performance": performance,
                },
            )
        # Keep completed-generation evidence across rollback, without provider replay data.
        visible_response.generation_diagnostics = {
            "_matoloop": {"performance": performance},
            "usage": {
                key: value for key, value in result.usage.items()
                if key in {
                    "input_tokens", "output_tokens", "total_tokens", "reasoning_tokens",
                    "cached_input_tokens", "cache_write_input_tokens",
                } and type(value) is int and value >= 0
            },
            **({"finish_reason": result.finish_reason} if result.finish_reason in {
                "stop", "tool_calls", "content_filter", *_OUTPUT_LIMIT_REASONS,
            } else {}),
        }
        if search_pending and not rejected_batch_code and (
            not normalized_calls
            or any(call.name not in offered_tool_names for call in normalized_calls)
        ):
            required_search_retries += 1
            if required_search_retries > REQUIRED_WEB_SEARCH_RETRIES:
                raise AgentRunError("agent.requiredWebSearchMissing")
            continue
        if memory_write_pending and not rejected_batch_code and (
            not normalized_calls
            or any(
                call.name not in offered_tool_names
                for call in normalized_calls
            )
        ):
            memory_write_retries += 1
            if memory_write_retries > REQUIRED_MEMORY_WRITE_RETRIES:
                raise AgentRunError("agent.requiredMemoryWriteMissing")
            continue
        if openscad_source_review_pending is not None and not rejected_batch_code:
            review_root, review_path = openscad_source_review_pending
            review_call = normalized_calls[0] if len(normalized_calls) == 1 else None
            if (
                review_call is None or not (
                    review_call.name == SCRIPT_TOOL and review_call.name in offered_tool_names
                    or review_call.name == "read" and review_call.arguments.get("root") == review_root
                    and review_call.arguments.get("path") == review_path
                )
            ):
                openscad_source_review_retries += 1
                if (
                    openscad_source_review_retries
                    > REQUIRED_OPENSCAD_SOURCE_REVIEW_RETRIES
                ):
                    raise AgentRunError(
                        "agent.requiredOpenScadSourceReviewMissing"
                    )
                continue
        serialized_calls = [
            {
                "id": call.id,
                "name": call.name,
                "arguments": agent_run_support._safe_tool_arguments(call.name, call.arguments),
                "toolDisplay": tool_display(
                    call.name,
                    agent_run_support._safe_tool_arguments(call.name, call.arguments),
                    effective_toolset.tools[call.name].definition
                    if effective_toolset is not None
                    and call.name in effective_toolset.tools
                    else None,
                ),
            }
            for call in normalized_calls
        ] or None
        if not normalized_calls and result.finish_reason in _OUTPUT_LIMIT_REASONS:
            raise AgentRunError("agent.outputLimitReached")
        assistant_content = canonical_assistant_content(result.content)
        if not assistant_content.strip() and visible_response.content.strip():
            assistant_content = canonical_assistant_content(visible_response.content)
        if not normalized_calls and (
            openscad_render_pending
            or (
                "render_openscad" in turn_tools.available_names
                and _OPENSCAD_FENCE_PATTERN.search(assistant_content)
            )
        ):
            openscad_render_pending = True
            openscad_render_retries += 1
            if openscad_render_retries > REQUIRED_OPENSCAD_RENDER_RETRIES:
                raise AgentRunError("agent.requiredOpenScadRenderMissing")
            continue
        if not normalized_calls and not assistant_content.strip():
            if tool_budget_exhausted or empty_response_retries >= EMPTY_RESPONSE_RETRIES:
                raise AgentRunError("agent.emptyResponse")
            empty_response_retries += 1
            logger.warning("Agent run %s received an empty model response; retrying", run.id)
            continue
        empty_response_retries = 0
        rejected_final_response = (
            tool_budget_exhausted and not normalized_calls
            and agent_run_support._has_simulated_tool_call(assistant_content)
        )
        assistant, continued = await persist_model_response(
            cls, db, run, chat_session, visible_response,
            result=result,
            calls=normalized_calls,
            serialized_calls=serialized_calls,
            content=assistant_content,
            trust_level=model_trust_level,
            search_pending=search_pending,
            memory_write_pending=memory_write_pending,
            openscad_render_pending=openscad_render_pending,
            openscad_source_review_pending=openscad_source_review_pending,
            rejected_batch_code=rejected_batch_code,
            rejected_final_response=rejected_final_response,
            tool_budget_exhausted=tool_budget_exhausted,
            performance=performance,
            memory_overview_runtime_state=memory_overview_runtime_state,
            streamed_reasoning_state=streamed_reasoning_state,
            research_used=research_used,
            research_sources=research_sources,
            research_source_titles=research_source_titles,
            live_tool_calls=live_tool_calls,
            hold_response=hold_response,
        )
        if continued:
            continuation_context_stale = True
            continue
        if rejected_final_response:
            raise AgentRunError("agent.toolCallLimitReached")
        if not normalized_calls:
            if has_children(run.id):
                await agent_run_support._publish_run_phase(
                    run.id, "processing_tool", tool="task", activityLabel="Subagenten arbeiten",
                )
                await wait_for_children(cls, run.id)
                continue
            await agent_run_support._publish_run_phase(run.id, "finalizing")
            return

        # ponytail: finish the issued batch before a redirected stop; explicit stops stay immediate.
        cls._tool_active_runs.add(run.id)
        try:
            batch = await execute_tool_batch(
                cls, db, run, chat_session, assistant, normalized_calls, serialized_calls,
                toolset=effective_toolset, offered_names=offered_tool_names,
                script_names=turn_tools.script_names,
                restore_script_arguments=result.restore_script_arguments,
                delegation_tool_names=turn_tools.available_names,
                root_files=root_files, memory_state=memory_state,
                model=model, provider=provider, ai_settings=ai_settings,
                runtime_settings=runtime_settings, tool_settings=tool_settings,
                limits=instance_limits, integration_settings=integration_settings,
                identity=agent_identity, agent_profile_id=agent_profile_id,
                skill_state=skill_state, execution_client=execution_client,
                records=records, projection=projection,
                trust_level=model_trust_level,
                excluded_memory_content_hashes=excluded_memory_content_hashes,
                delegated=delegated, max_tool_calls=tool_call_limit,
                tool_calls_used=tool_calls_used,
                recorded_tool_calls=recorded_tool_calls,
                interrupted_script_edits=interrupted_script_edits,
                execution_binding={
                    "executionAgentId": str(execution_client.agent_id) if execution_client else None,
                    "executionAgentAlias": selected_execution_agent.display_name if selected_execution_agent else None,
                },
                rejected_batch_code=rejected_batch_code,
                truncated_batch=truncated_batch,
                tool_batch_exceeds_budget=tool_batch_exceeds_budget,
                repeated_tool_call_repair_available=repeated_tool_call_repair_available,
                failed_tool_call_signatures=failed_tool_call_signatures,
                retried_transient_reads=retried_transient_reads,
                successful_research_calls=successful_research_calls,
                research_sources=research_sources, research_source_titles=research_source_titles,
                openscad_pending_paths=openscad_pending_paths,
                session_read_cursors=session_read_cursors,
                one_time_secrets=one_time_secrets,
                run_secret_refs=run_secret_refs,
                ephemeral_images=ephemeral_images, live_tool_calls=live_tool_calls,
                model_request=model_request, diagnostics_active=diagnostics_active,
                next_helper_request=next_helper_request,
                count_helper_request=count_helper_request,
                attachment_service=AttachmentService,
                research_tool_calls_used=research_tool_calls_used, research_used=research_used,
                openscad_render_pending=openscad_render_pending,
                openscad_render_retries=openscad_render_retries,
                openscad_source_review_pending=openscad_source_review_pending,
                openscad_source_review_retries=openscad_source_review_retries,
                repository_entries=repository_entries,
                repository_authorize=repository_authorize,
                pending_tool_repairs=pending_tool_repairs,
            )
        finally:
            cls._tool_active_runs.discard(run.id)
        if run.id in cls._redirected_runs and run.id in cls._user_cancelled_runs:
            raise asyncio.CancelledError
        tool_calls_used += batch.script_tool_calls_used
        recorded_tool_calls += batch.recorded_tool_calls
        read_evidence_available |= batch.read_evidence_available
        empty_script_rounds = (
            empty_script_rounds + 1 if batch.script_no_progress and batch.recorded_tool_calls == 0 else 0
        )
        research_tool_calls_used = batch.research_tool_calls_used
        research_used = batch.research_used
        openscad_render_pending = batch.openscad_render_pending
        openscad_render_retries = batch.openscad_render_retries
        review_source_was_pending = openscad_source_review_pending is not None
        openscad_source_review_pending = batch.openscad_source_review_pending
        openscad_source_review_retries = batch.openscad_source_review_retries
        repeated_tool_call_seen = batch.repeated_tool_call_seen
        repeated_tool_call_limit_reached = batch.repeated_tool_call_limit_reached
        user_denied_batch = batch.user_denied_batch
        memory_workflow_started = batch.memory_workflow_started
        memory_workflow_resolved = batch.memory_workflow_resolved
        memory_write_attempted = batch.memory_write_attempted
        resolved_memory_outcome = batch.resolved_memory_outcome
        required_search_called = batch.required_search_called
        required_search_succeeded = batch.required_search_succeeded
        required_search_error = batch.required_search_error
        skill_load_called = batch.skill_load_called
        skill_load_succeeded = batch.skill_load_succeeded
        if repeated_tool_call_seen:
            repeated_tool_call_repair_available = False
        if tool_budget_exhausted:
            raise AgentRunError("agent.toolCallLimitReached")
        if tool_batch_exceeds_budget:
            budget_finalization = True
            continue
        budget_finalization = tool_call_limit is not None and tool_calls_used >= tool_call_limit
        if truncated_batch:
            truncated_tool_batches += 1
            if not budget_finalization and truncated_tool_batches > TRUNCATED_TOOL_BATCH_RETRIES:
                raise AgentRunError("agent.outputLimitReached")
            continue
        if turn_tools.script_names and review_source_was_pending and openscad_source_review_pending is not None:
            openscad_source_review_retries += 1
            if openscad_source_review_retries > REQUIRED_OPENSCAD_SOURCE_REVIEW_RETRIES and not budget_finalization:
                raise AgentRunError("agent.requiredOpenScadSourceReviewMissing")
        if memory_workflow_resolved:
            memory_write_pending = False
            memory_write_retries = 0
            memory_write_outcome = resolved_memory_outcome
            if resolved_memory_outcome in {"stored", "forgotten"}:
                memory_cache.invalidate()
        elif memory_write_pending or memory_workflow_started:
            consume_retry = memory_write_pending or memory_write_attempted
            memory_write_pending = True
            memory_write_outcome = None
            if consume_retry:
                memory_write_retries += 1
            if memory_write_retries > REQUIRED_MEMORY_WRITE_RETRIES and not budget_finalization:
                raise AgentRunError("agent.requiredMemoryWriteMissing")
        if (repeated_tool_call_limit_reached or empty_script_rounds >= 2) and not budget_finalization:
            if (turn_tools.script_names and (research_used or read_evidence_available)
                    and (not required_search or required_search_completed)
                    and not (memory_write_pending or openscad_render_pending or openscad_source_review_pending)):
                script_finalization = True
                continue
            raise AgentRunError("tool.scriptNoProgress" if empty_script_rounds >= 2 else "agent.repeatedToolCall")
        if search_pending:
            if user_denied_batch:
                required_search_completed = True
                required_search_retries = 0
            elif required_search_succeeded:
                required_search_completed = True
                required_search_retries = 0
            elif required_search_called:
                required_search_retries += 1
                if required_search_retries > REQUIRED_WEB_SEARCH_RETRIES and not budget_finalization:
                    if required_search_error and required_search_error.startswith(
                        "integration."
                    ):
                        raise AgentRunError(required_search_error)
                    raise AgentRunError("agent.requiredWebSearchFailed")
            elif (skill_load_called or turn_tools.script_names) and not skill_load_succeeded:
                required_search_retries += 1
                if required_search_retries > REQUIRED_WEB_SEARCH_RETRIES and not budget_finalization:
                    raise AgentRunError("agent.requiredWebSearchMissing")
