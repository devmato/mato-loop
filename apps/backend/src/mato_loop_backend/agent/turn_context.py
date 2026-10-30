from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent import run_support
from mato_loop_backend.agent.context import ContextManager
from mato_loop_backend.agent.memory_context import MemoryTurn, memory_context_snapshot
from mato_loop_backend.agent.projection import (
    ContextError,
    ContextProjection,
    SummaryResult,
    model_browser_observations,
)
from mato_loop_backend.agent.prompts import automation_context_prompt, memory_context_prompt
from mato_loop_backend.events import run_events
from mato_loop_backend.media.attachments import AttachmentService
from mato_loop_backend.models import AgentRun, ChatSession, Message
from mato_loop_backend.providers.base import ProviderImageInput
from mato_loop_backend.providers.service import ProviderService
from mato_loop_backend.settings import AgentRuntimeSettings, AiSettings, InstanceLimits
from mato_loop_backend.settings.models import ModelConfig, TrustLevel


async def build_turn_context(
    runtime,
    db: AsyncSession,
    run: AgentRun,
    session: ChatSession,
    *,
    ai_settings: AiSettings,
    model: ModelConfig,
    trust_level: TrustLevel,
    settings: AgentRuntimeSettings,
    instance_limits: InstanceLimits,
    records: list[Message],
    system_prompt: str,
    runtime_instructions: str,
    schemas: list[dict[str, Any]],
    offered_tool_names: frozenset[str],
    memory_turn: MemoryTurn,
    memory_read_enabled: bool,
    request_options: dict[str, Any],
    live_tool_calls: dict[UUID, list[dict[str, Any]]],
    ephemeral_images: dict[tuple[UUID, int, str, int], ProviderImageInput],
    attachment_service: type[AttachmentService],
    tool_snapshots: list[dict[str, Any]] | None = None,
    session_automations: list[dict[str, Any]] | None = None,
    repository_context_loader: Callable[..., Awaitable[str]] | None = None,
    pdf_context: dict[str, dict] | None = None,
    runtime_context: str | None = None,
) -> tuple[ContextProjection, int, int]:
    async def summarize(source: str, output_tokens: int) -> SummaryResult:
        await run_support._publish_run_phase(run.id, "compacting_context")
        last_summary_publish_at = 0.0

        async def on_update(text: str, is_fallback: bool) -> None:
            nonlocal last_summary_publish_at
            now = time.monotonic()
            if text and not is_fallback and now - last_summary_publish_at < 0.1:
                return
            if text:
                last_summary_publish_at = now
            await run_events.publish(run.id, {
                "type": "context.summary.updated",
                "summary": {"content": text, "isFallback": is_fallback, "status": "streaming"},
            })

        return await runtime._summarize(
            source, output_tokens,
            ai_settings=ai_settings, active_model=model,
            runtime_settings=settings, on_update=on_update,
        )

    token_ratio = 1.0
    server_counts: dict[str, int] = {}
    counting_available: bool | None = None
    prepared_request = None

    async def prepare(messages: list[dict[str, Any]]):
        nonlocal prepared_request
        messages, images = run_support._project_ephemeral_images(
            model_browser_observations(messages), ephemeral_images,
            model=model, target_trust_level=trust_level,
        )
        prepared_request = await ProviderService.prepare_request(
            messages=messages, image_inputs=images, prepared_request=prepared_request, **request_options
        )
        return prepared_request

    async def count_request(prepared) -> int | None:
        nonlocal token_ratio
        fingerprint = prepared.input_token_fingerprint
        if fingerprint in server_counts:
            return server_counts[fingerprint]
        counted = await ProviderService.count_input_tokens(
            db, provider=request_options["provider"], prepared=prepared,
        )
        if counted is not None and fingerprint is not None:
            server_counts[fingerprint] = counted
            token_ratio = counted / max(1, prepared.estimated_input_tokens)
        return counted

    async def estimate_request(messages: list[dict[str, Any]]) -> int:
        nonlocal counting_available
        prepared = await prepare(messages)
        if counting_available is None:
            counted = await count_request(prepared)
            counting_available = counted is not None
            if counted is not None:
                return counted
        if counting_available:
            fingerprint = prepared.input_token_fingerprint
            if fingerprint in server_counts:
                return server_counts[fingerprint]
        return math.ceil(prepared.estimated_input_tokens * token_ratio)

    scope_payload = (
        {"total": memory_turn.scope.total, "pinned": memory_turn.scope.pinned}
        if memory_turn.scope is not None else None
    )
    compacted_summary = None
    selected_memory_snapshot = None
    for attempt in range(2):
        projection = None
        memory_count = 0
        episode_count = 0
        for candidate_index, (candidate_text, candidate_memory_count, candidate_episode_count) in enumerate(memory_turn.candidates):
            memory_context = memory_context_prompt(
                memory_text=candidate_text,
                memory_read_enabled=memory_read_enabled,
                memory_read_scope=trust_level,
                memory_scope=scope_payload,
                injected_memory_count=candidate_memory_count,
                injected_episode_count=candidate_episode_count,
                memory_overview_prefetched=memory_turn.overview_prefetched,
                memory_overview_has_more=memory_turn.overview_has_more,
                offered_tool_names=offered_tool_names,
            )
            try:
                projection = await ContextManager.build(
                    db,
                    chat_session=session,
                    records=records,
                    system_prompt=system_prompt,
                    runtime_instructions=runtime_instructions,
                    runtime_context=runtime_context,
                    memory_context=memory_context,
                    memory_contexts=memory_turn.historical_contexts,
                    tool_snapshots=tool_snapshots,
                    tool_schemas=schemas,
                    model=model,
                    target_trust_level=trust_level,
                    settings=settings,
                    attachment_service=attachment_service(settings=instance_limits, run_id=run.id),
                    pdf_context=pdf_context,
                    summarize=summarize,
                    estimate_request=estimate_request,
                    live_tool_calls=live_tool_calls,
                )
                memory_count = candidate_memory_count
                episode_count = candidate_episode_count
                selected_memory_snapshot = memory_context_snapshot(memory_context, candidate_text, session, trust_level)
                break
            except ContextError as exc:
                if exc.code != "agent.contextWindowExceeded" or candidate_index == len(memory_turn.candidates) - 1:
                    raise
        if projection is None:
            raise ContextError("agent.contextWindowExceeded")
        if projection.compacted:
            compacted_summary = projection.context_summary
        candidate_tokens = projection.estimated_input_tokens

        async def fits_context(context: str, projection: ContextProjection = projection) -> bool:
            nonlocal candidate_tokens
            messages = [{**projection.messages[0], "content": projection.messages[0]["content"] + "\n\n" + context},
                        *projection.messages[1:]]
            candidate_tokens = await estimate_request(messages)
            return candidate_tokens <= projection.input_budget_tokens

        if session_automations is not None:
            context = automation_context_prompt(session_automations)
            if not await fits_context(context):
                entries = [{
                    **{key: item[key] for key in ("id", "name", "revision", "enabled", "triggers", "nextRunAt") if key in item},
                    "taskExcerpt": item.get("task", "")[:240], "scheduleCount": len(item.get("schedules", [])),
                } for item in session_automations]
                context = automation_context_prompt(entries)
                while not await fits_context(context):
                    if not entries:
                        context = ""
                        break
                    # ponytail: halve the optional catalog; use prefix binary search if tighter budget utilization matters.
                    entries = entries[:len(entries) // 2]
                    context = automation_context_prompt(entries, omitted_count=len(session_automations) - len(entries))
            if context:
                projection.messages[0]["content"] += "\n\n" + context
                projection.estimated_input_tokens = candidate_tokens

        if repository_context_loader is not None:
            repository_context = await repository_context_loader(fits_context=fits_context)
            if repository_context:
                projection.messages[0]["content"] += "\n\n" + repository_context
                projection.estimated_input_tokens = candidate_tokens
        prepared = await prepare(projection.messages)
        if counting_available:
            final_count = await count_request(prepared)
            projection.input_token_source = "server-calibrated"
            if final_count is not None:
                if final_count > projection.input_budget_tokens:
                    if attempt == 0:
                        continue
                    raise ContextError("agent.contextWindowExceeded")
                projection.estimated_input_tokens = final_count
                projection.input_token_source = "server"
                projection.counted_input_fingerprint = prepared.input_token_fingerprint
        projection.prepared_request = prepared
        break
    current_user = next((item for item in reversed(records) if item.role == "user"), None)
    if current_user is not None and selected_memory_snapshot is not None:
        data = current_user.provider_data or {}
        metadata = data.get("_matoloop", {})
        if metadata.get("memoryContext") != selected_memory_snapshot:
            current_user.provider_data = {
                **data, "_matoloop": {**metadata, "memoryContext": selected_memory_snapshot},
            }
            await db.commit()
    if compacted_summary is not None:
        projection.compacted = True
        projection.context_summary = summary = compacted_summary
        await run_events.publish(run.id, {
            "type": "context.compacted",
            "estimatedInputTokens": projection.estimated_input_tokens,
            "summary": {
                "id": str(summary.id),
                "coveredThroughMessageId": str(summary.covered_through_message_id),
                "content": summary.content,
                "isFallback": summary.is_fallback,
                "createdAt": summary.created_at.isoformat() if summary.created_at else None,
            },
        })
    return projection, memory_count, episode_count
