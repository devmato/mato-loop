from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent import run_support
from mato_loop_backend.agent.activity import reasoning_activity_label
from mato_loop_backend.agent.projection import (
    ContextProjection,
    model_browser_observations,
)
from mato_loop_backend.agent.research import _research_snapshot
from mato_loop_backend.agent.run_contracts import _VisibleResponse
from mato_loop_backend.agent.run_support import GATED_DELTA_CHARACTERS, logger
from mato_loop_backend.diagnostics.service import (
    diagnostic_access_active,
    schedule_diagnostic_event,
)
from mato_loop_backend.events import run_events
from mato_loop_backend.memory.episode_indexing import EpisodeIndexService
from mato_loop_backend.models import AgentRun, ChatSession, Message
from mato_loop_backend.providers.base import (
    ProviderImageInput,
    ProviderResult,
    PublicReasoning,
    ToolCall,
)
from mato_loop_backend.providers.service import ProviderService
from mato_loop_backend.settings import AgentRuntimeSettings
from mato_loop_backend.settings.models import ModelConfig, TrustLevel


@dataclass
class ModelTurn:
    result: ProviderResult
    started_at: float
    duration_ms: int
    timing: dict[str, int | None]
    streamed_reasoning: dict[str, Any]
    diagnostics_active: bool


async def complete_model_turn(
    runtime,
    db: AsyncSession,
    run: AgentRun,
    session: ChatSession,
    visible_response: _VisibleResponse,
    *,
    projection: ContextProjection,
    ephemeral_images: dict[tuple[UUID, int, str, int], ProviderImageInput],
    model: ModelConfig,
    trust_level: TrustLevel,
    request_options: dict[str, Any],
    settings: AgentRuntimeSettings,
    schemas: list[dict[str, Any]],
    model_request: int,
    hold_response: bool,
) -> ModelTurn | None:
    visible_response.reset()
    started_at = time.monotonic()
    timing: dict[str, int | None] = {"firstReasoningMs": None, "firstTextMs": None}
    held_text: list[str] = []

    def mark_first_signal(key: str) -> None:
        if timing[key] is None:
            timing[key] = max(0, round((time.monotonic() - started_at) * 1_000))

    async def on_delta(text: str) -> None:
        mark_first_signal("firstTextMs")
        if hold_response:
            held_text.append(text)
            return
        visible_response.content_parts.append(text)
        await run_events.publish(run.id, {"type": "message.delta", "delta": text})

    streamed_reasoning: dict[str, Any] = {
        "text": "", "kind": "content", "summaryText": "",
        "summaryLabel": None, "lastPublished": 0.0,
    }

    async def on_reasoning(text: str, kind: str) -> None:
        if not text:
            return
        mark_first_signal("firstReasoningMs")
        streamed_reasoning["kind"] = kind
        streamed_reasoning["text"] += text
        summary_became_available = False
        if kind == "summary":
            streamed_reasoning["summaryText"] += text
            summary_label = reasoning_activity_label(
                {"kind": "summary", "text": streamed_reasoning["summaryText"]}
            )
            if summary_label != "Nachdenken":
                summary_became_available = streamed_reasoning["summaryLabel"] is None
                streamed_reasoning["summaryLabel"] = summary_label
        if hold_response:
            return
        now = time.monotonic()
        if now - streamed_reasoning["lastPublished"] < 0.1 and not summary_became_available:
            return
        streamed_reasoning["lastPublished"] = now
        payload = {
            "kind": streamed_reasoning["kind"], "text": streamed_reasoning["text"],
            "status": "streaming", "reasoningTokens": None,
            "observedDurationMs": None,
        }
        await run_events.publish(run.id, {
            "type": "reasoning.updated", "reasoning": payload,
            "activityLabel": streamed_reasoning["summaryLabel"] or reasoning_activity_label(payload),
        })
        visible_response.reasoning = payload

    async def on_warning(code: str) -> None:
        if code in visible_response.warnings:
            return
        visible_response.warnings.append(code)
        await run_events.publish(run.id, {
            "type": "run.warning", "code": code, "modelRequest": model_request,
        })

    await run_support._publish_run_phase(run.id, "waiting_model", modelRequest=model_request)
    diagnostics_active = await diagnostic_access_active(db, session.user_id)
    run._diagnostics_active = diagnostics_active

    async def on_provider_retry(
        attempt: int, total: int, status_code: int | None, error_code: str,
    ) -> None:
        logger.info(
            "Agent run %s retrying provider request (attempt %s/%s, status %s, code %s)",
            run.id, attempt, total, status_code, error_code,
        )
        await run_support._publish_run_phase(
            run.id, "waiting_model", modelRequest=model_request,
            providerAttempt=attempt, providerAttemptTotal=total,
        )
        if diagnostics_active:
            schedule_diagnostic_event(
                session.user_id, "model.retry", component="provider",
                session_id=session.id, run_id=run.id,
                correlation_id=f"model:{model_request}",
                payload={
                    "attempt": attempt, "totalAttempts": total,
                    "statusCode": status_code, "errorCode": error_code,
                },
            )

    request_messages, request_images = run_support._project_ephemeral_images(
        model_browser_observations(projection.messages), ephemeral_images,
        model=model, target_trust_level=trust_level,
    )
    correlation_id = f"model:{model_request}"
    if diagnostics_active:
        provider = request_options["provider"]
        schedule_diagnostic_event(
            session.user_id, "model.started", component="provider",
            session_id=session.id, run_id=run.id, correlation_id=correlation_id,
            payload={
                "providerId": str(provider.id), "providerName": provider.name,
                "adapterType": provider.adapter_type,
                "modelConfigId": str(model.id), "modelId": model.model_id,
                "trustLevel": trust_level.value,
                "reasoningEffort": request_options["reasoning_effort"],
                "toolChoice": request_options["tool_choice"],
                "timeoutSeconds": request_options["timeout"],
                "estimatedInputTokens": projection.estimated_input_tokens,
                "reservedOutputTokens": projection.reserved_output_tokens,
                "imageCount": len(request_images),
                "messages": request_messages, "tools": schemas,
            },
        )
    if await runtime._await_continuations(run.id):
        await runtime._resume_continuation(db, run, visible_response)
        return None
    runtime._provider_active_runs.add(run.id)
    try:
        result = await ProviderService.complete(
            db, **request_options,
            messages=request_messages, image_inputs=request_images,
            estimated_input_tokens=projection.estimated_input_tokens,
            counted_input_fingerprint=projection.counted_input_fingerprint,
            reserved_output_tokens=projection.reserved_output_tokens,
            safety_reserve_tokens=settings.safety_reserve_tokens,
            prepared_request=projection.prepared_request,
            on_delta=on_delta, on_reasoning=on_reasoning,
            on_warning=on_warning, on_retry=on_provider_retry,
        )
    except asyncio.CancelledError:
        if run.id in runtime._user_cancelled_runs or run.id not in runtime._pending_continuations:
            raise
        asyncio.current_task().uncancel()
        await runtime._await_continuations(run.id)
        await runtime._resume_continuation(db, run, visible_response)
        return None
    except Exception as exc:
        if diagnostics_active:
            schedule_diagnostic_event(
                session.user_id, "model.failed", component="provider",
                session_id=session.id, run_id=run.id, correlation_id=correlation_id,
                payload={"exceptionClass": type(exc).__name__},
            )
        raise
    finally:
        runtime._provider_active_runs.discard(run.id)
    if await runtime._await_continuations(run.id):
        await runtime._resume_continuation(db, run, visible_response)
        return None
    if hold_response and not result.content.strip():
        result.content = "".join(held_text)
    return ModelTurn(
        result, started_at, max(0, round((time.monotonic() - started_at) * 1_000)),
        timing, streamed_reasoning, diagnostics_active,
    )


async def persist_model_response(
    runtime,
    db: AsyncSession,
    run: AgentRun,
    session: ChatSession,
    visible_response: _VisibleResponse,
    *,
    result: ProviderResult,
    calls: list[ToolCall],
    serialized_calls: list[dict[str, Any]] | None,
    content: str,
    trust_level: TrustLevel,
    search_pending: bool,
    memory_write_pending: bool,
    openscad_render_pending: bool,
    openscad_source_review_pending: tuple[str, str] | None,
    rejected_batch_code: str | None,
    rejected_final_response: bool,
    tool_budget_exhausted: bool,
    performance: dict[str, Any],
    memory_overview_runtime_state: dict[str, Any] | None,
    streamed_reasoning_state: dict[str, Any],
    research_used: bool,
    research_sources: dict[str, str],
    research_source_titles: dict[str, str],
    live_tool_calls: dict[UUID, list[dict[str, Any]]],
    hold_response: bool,
) -> tuple[Message, bool]:
    starts_memory_workflow = any(
        call.name == "memory_write"
        or (call.name == "memory_retrieve" and call.arguments.get("purpose") == "write")
        for call in calls
    )
    hide_tool_round = (search_pending and bool(calls)) or (
        memory_write_pending or openscad_render_pending
        or openscad_source_review_pending is not None or starts_memory_workflow
        or bool(rejected_batch_code)
    )
    message_warnings = list(visible_response.warnings)
    if tool_budget_exhausted:
        message_warnings.append("agent.toolCallLimitReached")
    provider_data = run_support._safe_provider_data(result.provider_data, calls)
    provider_data.pop("output_budget", None)
    if hide_tool_round:
        provider_data = run_support._hidden_tool_round_provider_data(provider_data)
    if result.usage:
        provider_data["usage"] = result.usage
    if result.finish_reason:
        provider_data["finish_reason"] = result.finish_reason
    runtime_state = provider_data.get("_matoloop")
    if not isinstance(runtime_state, dict):
        runtime_state = {}
    provider_data["_matoloop"] = {
        **runtime_state,
        **run_support._run_metadata(run),
        **({"completionCause": "agent.toolCallLimitReached"} if tool_budget_exhausted else {}),
        "performance": performance,
        **({"warnings": message_warnings} if message_warnings else {}),
    }
    if memory_overview_runtime_state is not None:
        provider_data["_matoloop"] = {
            **provider_data["_matoloop"], "memoryOverview": memory_overview_runtime_state,
        }
    public_reasoning = (
        None if hide_tool_round else run_support._public_reasoning_payload(result.reasoning)
    )
    streamed_reasoning = streamed_reasoning_state["text"]
    if not hide_tool_round and public_reasoning is None and streamed_reasoning:
        public_reasoning = run_support._public_reasoning_payload(PublicReasoning(
            text=streamed_reasoning, kind=streamed_reasoning_state["kind"],
        ))
    public_research = None
    if not calls:
        public_research = _research_snapshot(
            policy=session.web_search_policy, used=research_used,
            content=content, sources=research_sources, titles=research_source_titles,
        )
        provider_data["_matoloop"] = {
            **provider_data["_matoloop"], "research": public_research,
        }
    assistant = Message(
        session_id=session.id, role="assistant",
        content="" if hide_tool_round else content,
        model_name=run.model_name, trust_level=trust_level.value,
        tool_calls=serialized_calls, provider_data=provider_data,
        public_reasoning=public_reasoning, created_at=datetime.now(UTC),
    )
    db.add(assistant)
    await db.flush()
    if calls:
        live_tool_calls[assistant.id] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in calls
        ]
    else:
        run.result_message_id = assistant.id
        await runtime._finalize_run_files(db, run, tolerate_conflict=rejected_final_response)
    await EpisodeIndexService.enqueue(db, assistant)
    await db.commit()
    if not calls and await runtime._await_continuations(run.id):
        metadata = (assistant.provider_data or {}).get("_matoloop", {})
        assistant.provider_data = {
            **(assistant.provider_data or {}),
            "_matoloop": {**metadata, "continuationSegment": True},
        }
        await db.commit()
        await run_events.publish(run.id, {
            "type": "message.updated", "messageId": str(assistant.id),
        })
        visible_response.reset()
        await runtime._resume_continuation(db, run, visible_response)
        return assistant, True
    if not calls:
        runtime._cancellable_runs.discard(run.id)
    visible_response.reset()
    if hold_response and not calls and assistant.content:
        for offset in range(0, len(assistant.content), GATED_DELTA_CHARACTERS):
            await run_events.publish(run.id, {
                "type": "message.delta",
                "delta": assistant.content[offset:offset + GATED_DELTA_CHARACTERS],
            })
    if public_reasoning:
        reasoning_label = reasoning_activity_label(public_reasoning)
        if reasoning_label == "Nachdenken" and streamed_reasoning_state["summaryLabel"]:
            reasoning_label = streamed_reasoning_state["summaryLabel"]
        await run_events.publish(run.id, {
            "type": "reasoning.updated", "reasoning": public_reasoning,
            "activityLabel": reasoning_label,
        })
    await run_events.publish(run.id, {
        "type": "message.completed",
        "message": {
            "id": str(assistant.id), "role": "assistant", "content": assistant.content,
            "createdAt": assistant.created_at.isoformat(),
            "modelName": assistant.model_name, "truncated": False,
            "generationStatus": assistant.generation_status,
            "toolCalls": serialized_calls, "reasoning": public_reasoning,
            "research": public_research, "warnings": message_warnings,
            "runId": str(run.id),
            "agentProfileId": str(run.agent_profile_id) if run.agent_profile_id else None,
        },
    })
    return assistant, False
