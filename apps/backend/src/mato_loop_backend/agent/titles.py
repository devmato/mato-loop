from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import selectinload

import mato_loop_backend.agent.run_support as agent_run_support
from mato_loop_backend.agent.run_contracts import AgentRunError
from mato_loop_backend.agent.run_support import logger
from mato_loop_backend.agent.title_generation import (
    TITLE_SYSTEM_PROMPT,
    initial_title_output_budget,
    repair_title_output_budget,
    title_input,
)
from mato_loop_backend.agent.title_generation import (
    generated_session_title as _generated_session_title,
)
from mato_loop_backend.db.session import AsyncSessionFactory
from mato_loop_backend.events import run_events
from mato_loop_backend.media.attachments import AttachmentError, AttachmentService
from mato_loop_backend.models import ChatSession, Message, TitleGenerationStatus
from mato_loop_backend.providers.base import (
    MAX_PROVIDER_IMAGE_BYTES,
    MAX_PROVIDER_IMAGES,
    ProviderImageInput,
)
from mato_loop_backend.providers.output_budget import output_token_limit
from mato_loop_backend.providers.service import ProviderService
from mato_loop_backend.settings import (
    AgentRuntimeSettings,
    SettingsStore,
    apply_session_ai,
    load_effective_ai,
)


async def generate_title(
    run_id: UUID,
    session_id: UUID,
    input_message_id: UUID,
    expected_title: str,
    *,
    after: asyncio.Task[None] | None = None,
) -> None:
    from mato_loop_backend.agent.runtime import AgentRuntime
    run_cancelled = False
    if after is not None:
        try:
            await asyncio.shield(asyncio.gather(after, return_exceptions=True))
            run_cancelled = after.cancelled()
        except asyncio.CancelledError:
            async with AsyncSessionFactory() as db:
                await AgentRuntime._fail_title_generation(db, session_id)
            raise
    async with AsyncSessionFactory() as db:
        try:
            chat_session = await db.get(ChatSession, session_id)
            if (
                chat_session is None
                or chat_session.title_generation_status
                != TitleGenerationStatus.RUNNING.value
            ):
                return
            if run_cancelled:
                chat_session.title_generation_status = TitleGenerationStatus.SKIPPED.value
                await db.commit()
                return
            if chat_session.title != expected_title:
                chat_session.title_generation_status = (
                    TitleGenerationStatus.SKIPPED.value
                )
                await db.commit()
                return
            ai_settings = apply_session_ai(
                await load_effective_ai(db, chat_session.user_id), chat_session
            )
            runtime_settings = await SettingsStore.load(db, AgentRuntimeSettings)
            input_message = await db.get(
                Message,
                input_message_id,
                options=(selectinload(Message.attachments),),
            )
            if (
                input_message is None
                or input_message.session_id != session_id
                or input_message.role != "user"
            ):
                raise AgentRunError("session.notFound")
            attachment_service = AttachmentService()
            attachment_content: list[tuple[str, str | None]] = []
            for attachment in input_message.attachments:
                filename = attachment.filename or "attachment"
                extracted_text = None
                if attachment.kind in {"text", "document"}:
                    try:
                        extracted_text = await asyncio.to_thread(
                            attachment_service.read_derived_text,
                            attachment,
                            session_id,
                        )
                    except AttachmentError:
                        pass
                attachment_content.append((filename, extracted_text))
            content = input_message.content
            if expected_title.startswith("Kopie: "):
                content = f"Previous conversation title: {expected_title.removeprefix('Kopie: ')}\n\n" + content
            request_content = title_input(content, attachment_content)
            explicit_model = ai_settings.title_generation_model_id is not None
            model_ids = (
                (ai_settings.title_generation_model_id,)
                if explicit_model
                else (chat_session.model_config_id, ai_settings.default_model_id)
            )
            title_model = next(
                (
                    candidate
                    for model_id in dict.fromkeys(model_ids)
                    if (candidate := agent_run_support._model_for_id(ai_settings, model_id)) is not None
                    and agent_run_support._provider_for_model(ai_settings, candidate) is not None
                ),
                None,
            )
            has_images = any(
                attachment.kind == "image" for attachment in input_message.attachments
            )
            model = title_model
            if (
                has_images
                and (model is None or not model.supports_image_input)
                and ai_settings.title_vision_fallback_enabled
            ):
                vision_model = agent_run_support._model_for_id(ai_settings, ai_settings.vision_model_id)
                if (
                    vision_model is not None
                    and vision_model.supports_image_input
                    and agent_run_support._provider_for_model(ai_settings, vision_model) is not None
                ):
                    model = vision_model
            if model is None:
                logger.warning(
                    "Title generation for session %s stopped without an available model",
                    session_id,
                )
                chat_session.title_generation_status = (
                    TitleGenerationStatus.FAILED.value
                )
                await db.commit()
                return
            provider = agent_run_support._provider_for_model(ai_settings, model)
            if provider is None:
                logger.warning(
                    "Title generation for session %s stopped without an available provider",
                    session_id,
                )
                chat_session.title_generation_status = (
                    TitleGenerationStatus.FAILED.value
                )
                await db.commit()
                return
            messages = [
                {
                    "role": "system",
                    "content": TITLE_SYSTEM_PROMPT,
                },
                {"role": "user", "content": request_content},
            ]
            image_inputs = []
            if has_images and model.supports_image_input:
                image_bytes = 0
                for attachment in input_message.attachments:
                    if (
                        attachment.kind != "image"
                        or len(image_inputs) >= MAX_PROVIDER_IMAGES
                        or image_bytes + attachment.size_bytes
                        > MAX_PROVIDER_IMAGE_BYTES
                    ):
                        continue
                    try:
                        image = await attachment_service.read_image(
                            attachment, session_id, current_only=True
                        )
                    except AttachmentError:
                        continue
                    image_bytes += len(image)
                    image_inputs.append(
                        ProviderImageInput(
                            1,
                            image,
                            input_message.trust_level,
                            attachment.mime_type,
                        )
                    )
            title = None
            model_limit = output_token_limit(model)
            output_budget = initial_title_output_budget(model_limit)
            for attempt in range(2):
                await AgentRuntime._ensure_user_active(db, chat_session.user_id)
                result = await ProviderService.complete(
                    db,
                    provider=provider,
                    model=model,
                    messages=messages,
                    tools=[],
                    timeout=runtime_settings.request_timeout_seconds,
                    max_output_tokens=output_budget,
                    safety_reserve_tokens=runtime_settings.safety_reserve_tokens,
                    image_inputs=image_inputs,
                )
                title = _generated_session_title(result.content)
                if title is not None:
                    break
                logger.warning(
                    "Title model returned no usable title for session %s "
                    "(attempt %s, visible characters: %s, reasoning present: %s, "
                    "finish reason: %s, output limit: %s)",
                    session_id,
                    attempt + 1,
                    len(result.content),
                    result.reasoning is not None,
                    result.finish_reason,
                    output_budget,
                )
                output_budget = repair_title_output_budget(
                    output_budget, model_limit, result
                )
                messages[0]["content"] += (
                    " The previous attempt contained no visible title. Return only the final "
                    "title in exactly one line. Do not include analysis, reasoning, tags, or "
                    "additional content."
                )
            if title is None:
                chat_session.title_generation_status = (
                    TitleGenerationStatus.FAILED.value
                )
                await db.commit()
                return
            updated_id = await db.scalar(
                update(ChatSession)
                .where(
                    ChatSession.id == session_id,
                    ChatSession.title == expected_title,
                    ChatSession.title_generation_status
                    == TitleGenerationStatus.RUNNING.value,
                )
                .values(
                    title=title,
                    title_generation_status=TitleGenerationStatus.COMPLETED.value,
                )
                .returning(ChatSession.id)
            )
            await db.commit()
            if updated_id is not None:
                await run_events.publish(
                    run_id,
                    {
                        "type": "session.title.updated",
                        "sessionId": str(session_id),
                        "title": title,
                    },
                )
        except asyncio.CancelledError:
            await AgentRuntime._fail_title_generation(db, session_id)
            raise
        except Exception:
            logger.warning(
                "Title generation failed for session %s",
                session_id,
                exc_info=True,
            )
            await AgentRuntime._fail_title_generation(db, session_id)
