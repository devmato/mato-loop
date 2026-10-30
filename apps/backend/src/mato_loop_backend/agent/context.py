from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import mato_loop_backend.agent.projection as agent_projection
from mato_loop_backend.agent.projection import (
    _SHORTENED_TOOL_OUTPUT,
    _SUMMARY_EVIDENCE_MARKER,
    ContextError,
    ContextProjection,
    SummaryResult,
)
from mato_loop_backend.media.attachments import AttachmentService
from mato_loop_backend.models import ChatSession, ContextSummary, Message
from mato_loop_backend.providers.output_budget import generation_reserve, raw_input_budget
from mato_loop_backend.settings.models import AgentRuntimeSettings, ModelConfig, TrustLevel
from mato_loop_backend.tools.snapshots import SNAPSHOT_MARKER

SummaryCallback = Callable[[str, int], Awaitable[SummaryResult]]


EstimateCallback = Callable[[list[dict[str, Any]]], Awaitable[int]]


def _model_order(records: list[Message]) -> list[Message]:
    """Keep completed tool batches together when direct messages arrive during execution."""
    ordered = []
    index = 0
    while index < len(records):
        assistant = records[index]
        ordered.append(assistant)
        index += 1
        if assistant.role != "assistant" or not assistant.tool_calls:
            continue
        run_id = (assistant.provider_data or {}).get("_matoloop", {}).get("runId")
        call_ids = {str(call["id"]) for call in assistant.tool_calls if call.get("id")}
        if not run_id or not call_ids:
            continue
        results = []
        direct_messages = []
        while index < len(records):
            item = records[index]
            if item.role == "tool" and item.tool_call_id in call_ids:
                results.append(item)
            elif (item.role == "user" and
                  (item.provider_data or {}).get("_matoloop", {}).get("continuationRunId") == run_id):
                direct_messages.append(item)
            else:
                break
            index += 1
        ordered.extend(results)
        ordered.extend(direct_messages)
    return ordered


class ContextManager:
    @staticmethod
    async def build(
        db: AsyncSession,
        *,
        chat_session: ChatSession,
        records: list[Message],
        system_prompt: str,
        tool_schemas: list[dict[str, Any]],
        model: ModelConfig,
        settings: AgentRuntimeSettings,
        summarize: SummaryCallback,
        estimate_request: EstimateCallback,
        target_trust_level: TrustLevel = TrustLevel.PUBLIC,
        memory_context: str | None = None,
        memory_contexts: dict[UUID, str] | None = None,
        runtime_instructions: str = "",
        runtime_context: str | None = None,
        live_tool_calls: dict[UUID, list[dict[str, Any]]] | None = None,
        attachment_service: AttachmentService | None = None,
        tool_snapshots: list[dict[str, Any]] | None = None,
        pdf_context: dict[str, dict] | None = None,
    ) -> ContextProjection:
        """Budget original projections with raw request estimates, before slack and safety."""
        # Reorder only the model's view; persisted timestamps and message order stay intact.
        records = _model_order([item for item in records if item.error_code is None])
        reserved_output_tokens = generation_reserve(model)
        input_budget = raw_input_budget(max(
            0,
            model.context_window_tokens
            - reserved_output_tokens
            - settings.safety_reserve_tokens,
        ))
        if input_budget <= 0:
            raise ContextError("agent.contextWindowTooSmall")
        trigger_tokens = min(
            input_budget,
            raw_input_budget(max(
                0,
                int(model.context_window_tokens * settings.compaction_trigger_ratio)
                - reserved_output_tokens
                - settings.safety_reserve_tokens,
            )),
        )
        latest = await db.scalar(
            select(ContextSummary)
            .where(ContextSummary.session_id == chat_session.id)
            .order_by(ContextSummary.created_at.desc(), ContextSummary.id.desc())
            .limit(1)
        )
        active_records = records
        if latest is not None:
            boundary = next(
                (
                    index
                    for index, item in enumerate(records)
                    if item.id == latest.covered_through_message_id
                ),
                None,
            )
            if boundary is not None:
                active_records = records[boundary + 1 :]
            else:
                latest = None

        characters_per_token = model.characters_per_token
        omitted_tool_result_ids: set[UUID] = set()
        pending_summary: ContextSummary | None = None

        async def persist_omissions() -> None:
            for item in active_records:
                if item.id in omitted_tool_result_ids:
                    data = item.provider_data or {}
                    item.provider_data = {
                        **data,
                        "_matoloop": {**data.get("_matoloop", {}), "contextOutputOmitted": True},
                    }
            if omitted_tool_result_ids or pending_summary is not None:
                await db.commit()

        async def project(
            summary: ContextSummary | None, selected: list[Message]
        ) -> tuple[list[dict[str, Any]], int]:
            # Coverage stays contiguous. The current request is a projection-only anchor, never
            # added back to active_records or summarized a second time after reload.
            current_user = next((item for item in reversed(records) if item.role == "user"), None)
            if current_user is not None and all(item.id != current_user.id for item in selected):
                selected = [current_user, *selected]
            attachment_characters = agent_projection.MAX_ATTACHMENT_EXCERPT_CHARACTERS
            while True:
                messages = await asyncio.to_thread(
                    agent_projection._projection_messages,
                    system_prompt,
                    summary,
                    selected,
                    target_trust_level,
                    memory_context=memory_context,
                    memory_contexts=memory_contexts,
                    tool_snapshots=tool_snapshots,
                    tool_snapshot_characters=int(input_budget * 0.05 * characters_per_token),
                    runtime_instructions=runtime_instructions,
                    include_attachment_text=not tool_schemas,
                    attachment_service=attachment_service,
                    pdf_context=pdf_context,
                    attachment_text_characters=attachment_characters,
                    available_records=records,
                    characters_per_token=characters_per_token,
                    historical_tool_output_tokens=settings.historical_tool_output_tokens,
                    live_tool_calls=live_tool_calls,
                    omitted_tool_result_ids=omitted_tool_result_ids,
                )
                if runtime_context:
                    messages.append({
                        "role": "user",
                        agent_projection._HOST_RUNTIME_CONTEXT: True,
                        "content": "<system-reminder>\nHost runtime state, not a new user request:\n"
                        + runtime_context + "\n</system-reminder>",
                    })
                total = await estimate_request(messages)
                if not pdf_context or total <= input_budget or attachment_characters == 0:
                    return messages, total
                attachment_characters = max(0, min(
                    attachment_characters // 2,
                    attachment_characters - int((total - input_budget) * characters_per_token),
                ))

        messages, estimated = await project(latest, active_records)
        if estimated <= max(1, trigger_tokens):
            agent_projection._strip_projection_metadata(messages)
            return ContextProjection(
                messages, estimated, input_budget,
                reserved_output_tokens=reserved_output_tokens,
            )

        async def estimate_minimum_request(indices: list[int]) -> int:
            if not indices:
                return estimated
            minimum = [dict(message) for message in messages]
            agent_projection._limit_tool_output_tokens(
                minimum,
                indices,
                0,
                characters_per_token,
                omitted_marker=_SHORTENED_TOOL_OUTPUT.lstrip(),
                fair_share=True,
            )
            # Retain privacy overhead for short bodies the real shortener cannot remove.
            return estimated if minimum == messages else await estimate_request(minimum)

        protected_start = agent_projection._compaction_boundary(
            active_records,
            agent_projection._protected_start(active_records, settings.protected_recent_turns),
            within_turn=estimated > trigger_tokens,
        )
        prefix = active_records[:protected_start]
        suffix = active_records[protected_start:]
        # Clear an old batch only under budget pressure, and never expand it on a later run.
        prefix_tool_names = agent_projection._tool_names_by_message_id(prefix)
        omitted_tool_result_ids.update(
            item.id for item in prefix if item.role == "tool"
            and prefix_tool_names.get(item.id) != "todo_write"
            and not (item.provider_data or {}).get("_matoloop", {}).get("contextOutputOmitted")
        )
        if omitted_tool_result_ids:
            messages, estimated = await project(latest, active_records)
            if estimated <= trigger_tokens:
                await persist_omissions()
                agent_projection._strip_projection_metadata(messages)
                return ContextProjection(
                    messages, estimated, input_budget,
                    reserved_output_tokens=reserved_output_tokens,
                )
        current_tool_indices = agent_projection._current_tool_output_indices(messages)
        minimum_request_tokens = await estimate_minimum_request(current_tool_indices)
        protected_tool_call_ids = {
            item.tool_call_id
            for item in suffix
            if item.role == "tool" and item.tool_call_id
        }
        protected_tool_indices = [
            index
            for index in current_tool_indices
            if messages[index].get("tool_call_id") in protected_tool_call_ids
            or messages[index].get(agent_projection._HOST_RETAINED_TOOL_RESULT)
        ]
        compaction_request_tokens = (
            minimum_request_tokens
            if protected_tool_indices == current_tool_indices
            else await estimate_minimum_request(protected_tool_indices)
        )
        compacted = False
        if (prefix or latest is not None) and compaction_request_tokens > trigger_tokens:
            target_tokens = max(256, int(input_budget * settings.compaction_target_ratio))
            summary_limit = max(256, min(2_048, target_tokens // 3))
            result = await summarize(
                agent_projection._summary_source(latest, prefix),
                summary_limit,
            )
            content = result.content.strip()
            if not content:
                raise ContextError("agent.compactionFailed")
            if prefix:
                covered_through_message_id = prefix[-1].id
            else:
                assert latest is not None
                covered_through_message_id = latest.covered_through_message_id
            covered_index = next(
                index for index, item in enumerate(records) if item.id == covered_through_message_id
            )
            content = content.split(_SUMMARY_EVIDENCE_MARKER, 1)[0].rstrip()
            content += agent_projection._summary_evidence(records[:covered_index + 1])
            pending_summary = ContextSummary(
                session_id=chat_session.id,
                covered_through_message_id=covered_through_message_id,
                previous_summary_id=latest.id if latest else None,
                content=content,
                estimated_tokens=agent_projection.estimate_tokens(content, characters_per_token),
                model_config_id=result.model_config_id,
                is_fallback=result.is_fallback,
            )
            latest = pending_summary
            messages, estimated = await project(latest, suffix)
            compacted = True
            current_tool_indices = agent_projection._current_tool_output_indices(messages)
            minimum_request_tokens = await estimate_minimum_request(current_tool_indices)

        current_tool_budget = max(
            0, trigger_tokens - minimum_request_tokens
        )
        agent_projection._limit_tool_output_tokens(
            messages,
            current_tool_indices,
            current_tool_budget,
            characters_per_token,
            omitted_marker=_SHORTENED_TOOL_OUTPUT.lstrip(),
            fair_share=True,
        )
        estimated = await estimate_request(messages)
        while estimated > trigger_tokens and current_tool_budget > 0:
            previous = estimated
            current_tool_budget = max(
                0, current_tool_budget - (estimated - trigger_tokens)
            )
            agent_projection._limit_tool_output_tokens(
                messages,
                current_tool_indices,
                current_tool_budget,
                characters_per_token,
                omitted_marker=_SHORTENED_TOOL_OUTPUT.lstrip(),
                fair_share=True,
            )
            estimated = await estimate_request(messages)
            if estimated >= previous:
                break

        if estimated > input_budget and any(message.get(SNAPSHOT_MARKER) for message in messages):
            messages = [message for message in messages if not message.get(SNAPSHOT_MARKER)]
            estimated = await estimate_request(messages)
        if estimated > input_budget:
            estimated = await agent_projection._prune_tool_outputs_to_budget(
                messages,
                estimate_request=estimate_request,
                characters_per_token=characters_per_token,
                input_budget=input_budget,
            )
        if estimated > input_budget:
            raise ContextError("agent.contextWindowExceeded")
        if pending_summary is not None:
            db.add(pending_summary)
            omitted_tool_result_ids.clear()
        await persist_omissions()
        agent_projection._strip_projection_metadata(messages)
        return ContextProjection(
            messages,
            estimated,
            input_budget,
            compacted=compacted,
            context_summary=pending_summary,
            reserved_output_tokens=reserved_output_tokens,
        )
