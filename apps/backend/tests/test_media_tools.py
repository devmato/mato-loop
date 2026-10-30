import json
import wave
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.media_prompt_profiles import (
    GPT_IMAGE_2_EDIT_INSTRUCTIONS,
    GPT_IMAGE_2_GENERATE_INSTRUCTIONS,
    Z_IMAGE_TURBO_GENERATE_INSTRUCTIONS,
    with_media_prompt_profiles,
)
from mato_loop_backend.files.service import RootFileService
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.models import ChatSession, Message, MessageAttachment
from mato_loop_backend.permissions.policy import PermissionDecision, media_permission
from mato_loop_backend.providers.base import ProviderError, ToolCall
from mato_loop_backend.providers.media import (
    MediaResult,
    MediaService,
    available_media_capabilities,
    media_model_available,
    resolve_media_target,
)
from mato_loop_backend.settings import (
    GENERAL_PROFILE_ID,
    AgentProfilesSettings,
    AiProviderSettings,
    AiSettings,
    IntegrationSettings,
    SettingsStore,
    ToolExecutionPolicy,
    ToolSettings,
)
from mato_loop_backend.settings.models import (
    AgentProfileAccessOverrides,
    LlmProviderConfig,
    ModelCapability,
    ModelConfig,
    ModelDefaults,
    ModelDefaultsSettings,
    TrustLevel,
)
from mato_loop_backend.tools.catalog import effective_tool_capabilities, tool_registry
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.media import _qr_code_png


def wav_bytes() -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8_000)
        audio.writeframes(b"\x00\x00" * 80)
    return buffer.getvalue()


def context(tmp_path, call: ToolCall, decision=PermissionDecision.ALLOW) -> ToolContext:
    session = ChatSession(
        id=uuid4(),
        user_id=uuid4(),
        title="Medien",
    )
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    return ToolContext(
        db=AsyncMock(spec=AsyncSession),
        run_id=uuid4(),
        session=session,
        tool_call=call,
        root_files=RootFileService(session.id, session_storage=storage),
        integration_settings=IntegrationSettings(),
        ask_handler=AsyncMock(return_value=decision),
    )


def media_settings(capability: ModelCapability) -> AiSettings:
    provider = LlmProviderConfig(
        name="Lemonade",
        adapter_type="lemonade",
        config={"base_url": "https://lemonade.test/v1"},
    )
    model = ModelConfig(
        provider_id=provider.id,
        model_id="media",
        capabilities={capability},
    )
    defaults = {
        ModelCapability.IMAGE_GENERATION: "image_generation_model_id",
        ModelCapability.SPEECH_GENERATION: "speech_generation_model_id",
        ModelCapability.AUDIO_GENERATION: "audio_generation_model_id",
        ModelCapability.AUDIO_TRANSCRIPTION: "audio_transcription_model_id",
    }
    return AiSettings(
        providers=[provider],
        models=[model],
        **{defaults[capability]: model.id},
    )


def codex_edit_settings() -> tuple[AiSettings, ModelConfig]:
    provider = LlmProviderConfig(
        name="Codex",
        adapter_type="openai-codex",
        credential_id=uuid4(),
    )
    chat = ModelConfig(provider_id=provider.id, model_id="openai-codex/gpt-test")
    image = ModelConfig(
        provider_id=provider.id,
        model_id="openai-codex/gpt-image-2",
        capabilities={
            ModelCapability.IMAGE_GENERATION,
            ModelCapability.IMAGE_EDITING,
        },
        discovery_metadata={"native_for_chat_provider": True},
    )
    return AiSettings(providers=[provider], models=[chat, image]), chat


def mock_settings_load(monkeypatch, tools: ToolSettings, ai: AiSettings) -> AsyncMock:
    async def load(_db, settings_type, **_kwargs):
        return tools if settings_type is ToolSettings else ai if settings_type is AiProviderSettings else settings_type()

    mocked = AsyncMock(side_effect=load)
    monkeypatch.setattr(SettingsStore, "load", mocked)
    async def load_optional(_db, settings_type, **_kwargs):
        if settings_type is ModelDefaultsSettings:
            return ModelDefaultsSettings(**ai.model_dump(include=set(ModelDefaults.model_fields)))
        return (
            _profile_settings(tools, ai)
            if settings_type is AgentProfilesSettings
            else None
        )

    monkeypatch.setattr(SettingsStore, "load_optional", load_optional)
    return mocked


def _profile_settings(tools: ToolSettings, ai: AiSettings) -> AgentProfilesSettings:
    return AgentProfilesSettings(
        builtin_access_overrides={
            GENERAL_PROFILE_ID: AgentProfileAccessOverrides(
                private_permissions=tools.private_permissions,
                public_permissions=tools.public_permissions,
            )
        }
    )


def test_media_schemas_follow_effective_capabilities() -> None:
    settings = ToolSettings.model_validate(
        {
            "privatePermissions": {
                "policies": {"media.generate.speech": "disabled"}
            },
        }
    )
    ai = media_settings(ModelCapability.IMAGE_GENERATION)
    capabilities = effective_tool_capabilities(
        settings, available_media_capabilities(ai)
    )
    schemas = tool_registry.schemas(
        IntegrationSettings(), tool_capabilities=capabilities
    )
    names = {item["name"] for item in schemas}

    assert capabilities == {
        "media.generate.image",
        "settings.agent_identity.update",
    }
    assert "generate_image" in names
    assert "customize_matoloop" in names
    assert "generate_speech" not in names
    assert "generate_audio" not in names
    assert "transcribe_audio" not in names


def test_identity_availability_does_not_hide_video_analysis() -> None:
    capabilities = effective_tool_capabilities(
        ToolSettings(), video_available=True, identity_update_available=False
    )

    assert "media.analyze.video" in capabilities
    assert "settings.agent_identity.update" not in capabilities


async def test_disabled_media_tool_is_rejected_before_provider(
    tmp_path, monkeypatch
) -> None:
    call = ToolCall(id="image", name="generate_image", arguments={"prompt": "Test"})
    tool_context = context(tmp_path, call)
    tool_context.tool_settings.permissions.policies["media.generate.image"] = (
        ToolExecutionPolicy.DISABLED
    )
    mock_settings_load(
        monkeypatch,
        ToolSettings(
            private_permissions={
                "policies": {"media.generate.image": "disabled"}
            },
            public_permissions={
                "policies": {"media.generate.image": "disabled"}
            },
        ),
        media_settings(ModelCapability.IMAGE_GENERATION),
    )
    generate = AsyncMock(side_effect=AssertionError("Provider must not be called"))
    monkeypatch.setattr(MediaService, "generate_image", generate)

    result = await tool_registry.execute(call, tool_context)

    assert result.is_error is True
    assert json.loads(result.content) == {
        "error": {
            "code": "tool.unavailable",
                "message": "The tool is unavailable.",
        }
    }
    generate.assert_not_awaited()


@pytest.mark.parametrize("policy", [ToolExecutionPolicy.ASK, ToolExecutionPolicy.DISABLED])
async def test_media_dispatch_retains_private_spawn_ceiling_after_capability_reload(tmp_path, monkeypatch, policy):
    from mato_loop_backend.agent.subagents import permission_snapshot

    call = ToolCall("image", "generate_image", {"prompt": "Test"})
    tool_context = context(tmp_path, call, decision=PermissionDecision.DENY)
    tool_context.model_trust_level = TrustLevel.PRIVATE
    before = ToolSettings(
        private_permissions={"policies": {"media.generate.image": policy}}
    ).select_trust(TrustLevel.PRIVATE)
    snapshot = permission_snapshot(before, {"generate_image"})
    live = ToolSettings()
    tool_context.tool_settings = live.model_copy(deep=True)
    tool_context.tool_settings._delegation_ceiling = snapshot
    mock_settings_load(monkeypatch, live, media_settings(ModelCapability.IMAGE_GENERATION))
    generate = AsyncMock(side_effect=AssertionError("Unapproved media must not be generated"))
    monkeypatch.setattr(MediaService, "generate_image", generate)
    effective = tool_registry.effective(IntegrationSettings(), tool_settings=live, tool_capabilities={"media.generate.image"})
    result = await effective.execute(call, tool_context)
    assert result.metadata["errorCode"] == ("permission.denied" if policy == ToolExecutionPolicy.ASK else "tool.unavailable")
    assert tool_context.tool_settings._delegation_ceiling == snapshot
    if policy == ToolExecutionPolicy.ASK:
        tool_context.ask_handler.assert_awaited_once()
    generate.assert_not_awaited()


async def test_video_tool_revalidation_keeps_frame_analysis_available(
    tmp_path, monkeypatch
) -> None:
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(
        provider_id=provider.id,
        model_id="vision",
        supports_image_input=True,
    )
    ai = AiSettings(providers=[provider], models=[model], video_model_id=model.id)
    call = ToolCall(id="video", name="analyze_video", arguments={})
    tool_context = context(tmp_path, call)
    tool_context.model_id = model.id
    mock_settings_load(monkeypatch, ToolSettings(), ai)

    result = await tool_registry.execute(call, tool_context)

    assert result.metadata == {"errorCode": "tool.invalidArguments", "argumentsRejected": True}


async def test_ask_denial_stops_before_media_service(tmp_path, monkeypatch) -> None:
    settings = ToolSettings(
        private_permissions={"policies": {"media.generate.image": "ask"}},
        public_permissions={"policies": {"media.generate.image": "ask"}},
    )
    call = ToolCall(id="image", name="generate_image", arguments={"prompt": "Test"})
    tool_context = context(tmp_path, call, PermissionDecision.DENY)
    ai = media_settings(ModelCapability.IMAGE_GENERATION)
    mock_settings_load(monkeypatch, settings, ai)
    generate = AsyncMock(side_effect=AssertionError("Provider must not be called"))
    monkeypatch.setattr(MediaService, "generate_image", generate)

    result = await tool_registry.execute(call, tool_context)

    assert result.metadata == {"errorCode": "permission.denied"}
    generate.assert_not_awaited()
    request = tool_context.ask_handler.await_args.args[0]
    provider, model = ai.providers[0], ai.models[0]
    assert request == media_permission(
        tool_context.session.id,
        "media.generate.image",
        "generate_image",
        provider_id=provider.id,
        model_id=model.id,
        model_name=model.model_id,
        trust_level="public",
    )


async def test_generated_image_is_returned_as_an_attachment(
    tmp_path, monkeypatch
) -> None:
    settings = ToolSettings()
    call = ToolCall(id="image", name="generate_image", arguments={"prompt": "Test"})
    tool_context = context(tmp_path, call)
    ai = media_settings(ModelCapability.IMAGE_GENERATION)
    tool_context.model_id = ai.models[0].id
    mock_settings_load(monkeypatch, settings, ai)
    monkeypatch.setattr(
        MediaService,
        "generate_image",
        AsyncMock(return_value=MediaResult(b"image", "image/png", "generated.png")),
    )

    result = await tool_registry.execute(call, tool_context)

    assert result.attachments[0].filename == "generated.png"
    assert result.attachments[0].data == b"image"


def test_image_target_prefers_native_provider_model_over_global_default() -> None:
    codex = LlmProviderConfig(
        name="Codex",
        adapter_type="openai-codex",
        credential_id=uuid4(),
    )
    lemonade = LlmProviderConfig(name="Lemonade", adapter_type="lemonade")
    chat = ModelConfig(provider_id=codex.id, model_id="openai-codex/gpt-test")
    native = ModelConfig(
        provider_id=codex.id,
        model_id="openai-codex/gpt-image-2",
        capabilities={
            ModelCapability.IMAGE_GENERATION,
            ModelCapability.IMAGE_EDITING,
        },
        discovery_metadata={"native_for_chat_provider": True},
    )
    fallback = ModelConfig(
        provider_id=lemonade.id,
        model_id="Z-Image-Turbo",
        capabilities={ModelCapability.IMAGE_GENERATION},
    )
    settings = AiSettings(
        providers=[codex, lemonade],
        models=[chat, native, fallback],
        image_generation_model_id=fallback.id,
    )
    impostor = ModelConfig(
        provider_id=codex.id,
        model_id="openai-codex/not-an-image-model",
        capabilities={ModelCapability.IMAGE_GENERATION},
    )
    settings.models.append(impostor)

    assert not media_model_available(
        settings, ModelCapability.IMAGE_GENERATION, impostor.id
    )

    assert resolve_media_target(
        settings,
        ModelCapability.IMAGE_GENERATION,
        active_model_id=chat.id,
    ) == (codex, native)
    assert resolve_media_target(
        settings,
        ModelCapability.IMAGE_EDITING,
        active_model_id=chat.id,
    ) == (codex, native)

    native.enabled = False
    assert resolve_media_target(
        settings,
        ModelCapability.IMAGE_GENERATION,
        active_model_id=chat.id,
    ) == (lemonade, fallback)


def test_media_prompt_profiles_follow_offered_resolved_target() -> None:
    codex = LlmProviderConfig(
        name="Codex",
        adapter_type="openai-codex",
        credential_id=uuid4(),
    )
    lemonade = LlmProviderConfig(name="Lemonade", adapter_type="lemonade")
    chat = ModelConfig(provider_id=codex.id, model_id="openai-codex/gpt-test")
    native = ModelConfig(
        provider_id=codex.id,
        model_id="openai-codex/gpt-image-2",
        capabilities={
            ModelCapability.IMAGE_GENERATION,
            ModelCapability.IMAGE_EDITING,
        },
        discovery_metadata={"native_for_chat_provider": True},
    )
    z_image = ModelConfig(
        provider_id=lemonade.id,
        model_id="Z-Image-Turbo",
        capabilities={ModelCapability.IMAGE_GENERATION},
    )
    settings = AiSettings(
        providers=[codex, lemonade],
        models=[chat, native, z_image],
        image_generation_model_id=z_image.id,
    )
    offered = tool_registry.effective(
        IntegrationSettings(),
        tool_capabilities={"media.generate.image", "media.edit.image"},
    ).only("generate_image", "edit_image")

    profiled = with_media_prompt_profiles(
        offered, settings, active_model_id=chat.id
    )

    assert profiled.prompt_fragments == (
        GPT_IMAGE_2_GENERATE_INSTRUCTIONS,
        GPT_IMAGE_2_EDIT_INSTRUCTIONS,
    )
    assert profiled.tools["generate_image"].selected_options["media_model_id"] == str(
        native.id
    )

    native.enabled = False
    generation_only = offered.only("generate_image")
    profiled = with_media_prompt_profiles(
        generation_only, settings, active_model_id=chat.id
    )

    assert profiled.prompt_fragments == (Z_IMAGE_TURBO_GENERATE_INSTRUCTIONS,)
    assert profiled.tools["generate_image"].selected_options == {
        "media_provider_id": str(lemonade.id),
        "media_model_id": str(z_image.id),
        "media_adapter_type": "lemonade",
        "media_provider_model_id": "Z-Image-Turbo",
    }


def test_lemonade_does_not_advertise_unimplemented_image_editing() -> None:
    provider = LlmProviderConfig(name="Lemonade", adapter_type="lemonade")
    model = ModelConfig(
        provider_id=provider.id,
        model_id="manual-edit-model",
        capabilities={ModelCapability.IMAGE_EDITING},
    )
    settings = AiSettings(
        providers=[provider],
        models=[model],
        image_generation_model_id=model.id,
    )

    assert ModelCapability.IMAGE_EDITING not in available_media_capabilities(settings)


async def test_media_tool_rejects_target_changed_after_prompt_binding(
    tmp_path, monkeypatch
) -> None:
    codex = LlmProviderConfig(
        name="Codex",
        adapter_type="openai-codex",
        credential_id=uuid4(),
    )
    lemonade = LlmProviderConfig(
        name="Lemonade",
        adapter_type="lemonade",
        config={"base_url": "https://lemonade.test/v1"},
    )
    chat = ModelConfig(provider_id=codex.id, model_id="openai-codex/gpt-test")
    native = ModelConfig(
        provider_id=codex.id,
        model_id="openai-codex/gpt-image-2",
        capabilities={ModelCapability.IMAGE_GENERATION},
        discovery_metadata={"native_for_chat_provider": True},
    )
    z_image = ModelConfig(
        provider_id=lemonade.id,
        model_id="Z-Image-Turbo",
        capabilities={ModelCapability.IMAGE_GENERATION},
    )
    initial = AiSettings(
        providers=[codex, lemonade],
        models=[chat, native, z_image],
        image_generation_model_id=z_image.id,
    )
    changed = initial.model_copy(deep=True)
    changed.models[1].enabled = False
    tools = ToolSettings()
    offered = tool_registry.effective(
        IntegrationSettings(),
        tool_capabilities={"media.generate.image"},
        tool_settings=tools,
    ).only("generate_image")
    bound = with_media_prompt_profiles(offered, initial, active_model_id=chat.id)
    call = ToolCall(id="image", name="generate_image", arguments={"prompt": "Test"})
    tool_context = context(tmp_path, call)
    tool_context.model_id = chat.id
    mock_settings_load(monkeypatch, tools, changed)
    generate = AsyncMock(side_effect=AssertionError("Provider must not be called"))
    monkeypatch.setattr(MediaService, "generate_image", generate)

    result = await bound.execute(call, tool_context)

    assert result.metadata == {"errorCode": "tool.unavailable"}
    generate.assert_not_awaited()
    tool_context.ask_handler.assert_not_awaited()


async def test_edit_image_sends_ordered_session_images_and_scopes_approval(
    tmp_path, monkeypatch
) -> None:
    attachment_ids = [uuid4(), uuid4()]
    original_attachment_id = uuid4()
    call = ToolCall(
        id="edit",
        name="edit_image",
        arguments={
            "prompt": "Change only the background; keep both subjects unchanged.",
            "images": [{"root": "files", "path": f"image-{index}.png"} for index in range(2)],
        },
    )
    tool_context = context(tmp_path, call)
    ai, chat = codex_edit_settings()
    tool_context.model_id = chat.id
    tools = ToolSettings(private_permission_preset="ask_all", public_permission_preset="ask_all")
    mock_settings_load(monkeypatch, tools, ai)
    tool_context.db.get.return_value = SimpleNamespace(user_id=None)
    message = Message(
        id=uuid4(),
        session_id=tool_context.session.id,
        role="user",
        content="Bearbeite diese Bilder",
        trust_level="public",
    )
    images = [_qr_code_png("first"), _qr_code_png("second")]
    revisions = [tool_context.root_files.create_output_file(f"image-{index}.png", data)
                 for index, data in enumerate(images)]
    call.arguments["images"] = [{"root": "files", "path": revision.path} for revision in revisions]
    attachments = [
        MessageAttachment(
            id=attachment_id,
            message_id=message.id,
            message=message,
            position=position,
            kind="image",
            category="generated" if position == 0 else "user_upload",
            purpose="artifact" if position == 0 else "attachment",
            processing_status="ready",
            mime_type="image/png",
            size_bytes=revisions[position].size_bytes,
            sha256=revisions[position].sha256,
            file_root_id=tool_context.session.id,
            file_id=revisions[position].file_id,
            file_revision=revisions[position].revision,
            file_path=revisions[position].path,
            metadata_json=(
                {"authorizationSourceAttachmentIds": [str(original_attachment_id)]}
                if position == 0
                else {}
            ),
        )
        for position, attachment_id in enumerate(attachment_ids)
    ]
    scalars = MagicMock()
    scalars.all.return_value = list(reversed(attachments))
    tool_context.db.scalars.return_value = scalars
    tool_context.db.scalar.side_effect = attachments
    edit = AsyncMock(
        return_value=MediaResult(
            b"result",
            "image/png",
            "generated.png",
            trust_level=TrustLevel.PUBLIC,
        )
    )
    monkeypatch.setattr(MediaService, "edit_image", edit)

    result = await tool_registry.execute(call, tool_context)

    assert result.is_error is False
    assert result.content == "Image edited."
    assert edit.await_args.kwargs["images"] == (
        (images[0], "image/png"),
        (images[1], "image/png"),
    )
    request = tool_context.ask_handler.await_args.args[0]
    scope = json.loads(request.resource_scope)
    assert request.capability == "media.edit.image"
    assert scope["sourceAttachmentIds"] == sorted(
        [str(original_attachment_id), str(attachment_ids[1])]
    )
    assert edit.await_args.kwargs["source_attachment_ids"] == tuple(attachment_ids)
    assert result.attachments[0].metadata["authorizationSourceAttachmentIds"] == sorted(
        [str(original_attachment_id), str(attachment_ids[1])]
    )
    assert result.attachments[0].file_root_id is None
    assert result.attachments[0].file_id is None


@pytest.mark.parametrize(
    ("category", "purpose", "reuses_identity"),
    [
        ("generated", "artifact", True),
        ("user_upload", "attachment", False),
        ("downloaded", "artifact", False),
    ],
)
async def test_edit_image_sends_authorized_private_sources_to_public_target(
    tmp_path, monkeypatch, category, purpose, reuses_identity
) -> None:
    attachment_id = uuid4()
    call = ToolCall(
        id="edit",
        name="edit_image",
        arguments={"prompt": "Change the background", "images": [{"root": "files", "path": "image.png"}]},
    )
    tool_context = context(tmp_path, call)
    ai, chat = codex_edit_settings()
    tool_context.model_id = chat.id
    tools = ToolSettings.model_validate(
        {
            "publicPermissions": {
                "policies": {"media.edit.image": "auto"}
            },
        }
    )
    mock_settings_load(monkeypatch, tools, ai)
    tool_context.db.get.return_value = SimpleNamespace(user_id=None)
    message = Message(
        id=uuid4(),
        session_id=tool_context.session.id,
        role="user",
        content="Privat",
        trust_level="private",
    )
    image = _qr_code_png("private")
    revision = tool_context.root_files.create_output_file("image.png", image)
    call.arguments["images"][0]["path"] = revision.path
    file_root_id, file_id = tool_context.session.id, revision.file_id
    attachment = MessageAttachment(
        id=attachment_id,
        message_id=message.id,
        message=message,
        position=0,
        kind="image",
        category=category,
        purpose=purpose,
        processing_status="ready",
        mime_type="image/png",
        size_bytes=revision.size_bytes,
        sha256=revision.sha256,
        file_root_id=file_root_id,
        file_id=file_id,
        file_revision=1,
        file_path=revision.path,
    )
    scalars = MagicMock()
    scalars.all.return_value = [attachment]
    tool_context.db.scalars.return_value = scalars
    tool_context.db.scalar.return_value = attachment
    edit = AsyncMock(
        return_value=MediaResult(
            b"result",
            "image/png",
            "generated.png",
            trust_level=TrustLevel.PUBLIC,
        )
    )
    monkeypatch.setattr(MediaService, "edit_image", edit)

    result = await tool_registry.execute(call, tool_context)

    assert result.is_error is False
    assert edit.await_args.kwargs["images"] == ((image, "image/png"),)
    assert result.attachments[0].file_root_id == (file_root_id if reuses_identity else None)
    assert result.attachments[0].file_id == (file_id if reuses_identity else None)
    assert [call.args[0].capability for call in tool_context.ask_handler.await_args_list] == ["session.files.read"]


async def test_policy_revocation_during_approval_stops_media_service(
    tmp_path, monkeypatch
) -> None:
    ask_settings = ToolSettings(
        private_permissions={"policies": {"media.generate.image": "ask"}},
        public_permissions={"policies": {"media.generate.image": "ask"}},
    )
    call = ToolCall(id="image", name="generate_image", arguments={"prompt": "Test"})
    tool_context = context(tmp_path, call)
    ai = media_settings(ModelCapability.IMAGE_GENERATION)
    async def load(_db, settings_type, **_kwargs):
        if settings_type is AiProviderSettings:
            return ai
        return ask_settings if settings_type is ToolSettings else settings_type()

    disabled_settings = ToolSettings(
        private_permissions={"policies": {"media.generate.image": "disabled"}},
        public_permissions={"policies": {"media.generate.image": "disabled"}},
    )
    profile_loads = 0

    async def load_optional(_db, settings_type, **_kwargs):
        nonlocal profile_loads
        if settings_type is ModelDefaultsSettings:
            return ModelDefaultsSettings(**ai.model_dump(include=set(ModelDefaults.model_fields)))
        if settings_type is not AgentProfilesSettings:
            return None
        profile_loads += 1
        return _profile_settings(
            ask_settings if profile_loads == 1 else disabled_settings,
            ai,
        )

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    monkeypatch.setattr(SettingsStore, "load_optional", load_optional)
    generate = AsyncMock(side_effect=AssertionError("Provider must not be called"))
    monkeypatch.setattr(MediaService, "generate_image", generate)

    result = await tool_registry.execute(call, tool_context)

    assert result.metadata == {"errorCode": "tool.unavailable"}
    generate.assert_not_awaited()
    tool_context.ask_handler.assert_awaited_once()


async def test_auto_media_tool_returns_typed_attachment(tmp_path, monkeypatch) -> None:
    settings = ToolSettings.model_validate(
        {
            "generateSpeech": {"timeoutSeconds": 125},
            "publicPermissions": {
                "policies": {"media.generate.speech": "auto"}
            },
        }
    )
    call = ToolCall(id="speech", name="generate_speech", arguments={"text": "Hallo"})
    tool_context = context(tmp_path, call)
    mock_settings_load(
        monkeypatch, settings, media_settings(ModelCapability.SPEECH_GENERATION)
    )
    generate = AsyncMock(return_value=MediaResult(wav_bytes(), "audio/wav", "speech.wav"))
    monkeypatch.setattr(MediaService, "generate_speech", generate)

    result = await tool_registry.execute(call, tool_context)

    assert result.is_error is False
    assert result.attachments[0].kind == "audio"
    assert result.attachments[0].filename == "speech.wav"
    assert result.attachments[0].presentation == "result"
    tool_context.ask_handler.assert_not_awaited()
    generate.assert_awaited_once()
    assert generate.await_args.kwargs["timeout"] == 125


async def test_media_timeout_is_returned_to_model_with_retry_context(
    tmp_path, monkeypatch
) -> None:
    settings = ToolSettings.model_validate(
        {"generateImage": {"timeoutSeconds": 640}}
    )
    call = ToolCall(id="image", name="generate_image", arguments={"prompt": "Test"})
    tool_context = context(tmp_path, call)
    mock_settings_load(
        monkeypatch, settings, media_settings(ModelCapability.IMAGE_GENERATION)
    )
    generate = AsyncMock(
        side_effect=ProviderError(
            "timed out",
            code="provider.timeout",
            params={"timeoutSeconds": 640, "retryable": True},
        )
    )
    monkeypatch.setattr(MediaService, "generate_image", generate)

    result = await tool_registry.execute(call, tool_context)

    assert result.is_error is True
    assert json.loads(result.content) == {
        "error": {
            "code": "provider.timeout",
            "message": "The model provider request could not be completed.",
            "params": {"timeoutSeconds": 640, "retryable": True},
        }
    }
    assert generate.await_args.kwargs["timeout"] == 640


async def test_transcription_cannot_read_path_outside_session(
    tmp_path, monkeypatch
) -> None:
    settings = ToolSettings(
        private_permissions={"policies": {"media.transcribe.audio": "auto"}},
        public_permissions={"policies": {"media.transcribe.audio": "auto"}},
    )
    call = ToolCall(
        id="transcribe",
        name="transcribe_audio",
        arguments={"root": "files", "path": "../other-session/audio.wav"},
    )
    tool_context = context(tmp_path, call)
    tool_context.db.scalar.return_value = None
    tool_context.db.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))
    mock_settings_load(
        monkeypatch, settings, media_settings(ModelCapability.AUDIO_TRANSCRIPTION)
    )
    transcribe = AsyncMock(side_effect=AssertionError("Provider must not be called"))
    monkeypatch.setattr(MediaService, "transcribe_audio", transcribe)

    result = await tool_registry.execute(call, tool_context)

    assert result.metadata == {"errorCode": "file.invalidPath"}
    transcribe.assert_not_awaited()


@pytest.mark.parametrize("name", ["read_qr_code", "transcribe_audio", "edit_image"])
@pytest.mark.parametrize("location", ["files", "computer_relative", "computer_absolute"])
async def test_media_tools_read_files_without_chat_attachments(tmp_path, monkeypatch, name, location):
    filename = "audio.wav" if name == "transcribe_audio" else "image.png"
    data = wav_bytes() if name == "transcribe_audio" else _qr_code_png("file input")
    reference = {
        "root": "files" if location == "files" else "computer",
        "path": f"/work/{filename}" if location == "computer_absolute" else filename,
    }
    arguments = {"images": [reference], "prompt": "Make it blue"} if name == "edit_image" else reference
    call = ToolCall("media", name, arguments)
    tool_context = context(tmp_path, call)
    tool_context.db.scalar.return_value = None
    tool_context.db.get.return_value = SimpleNamespace(user_id=None)
    if location == "files":
        tool_context.root_files.create_file("session", filename, BytesIO(data), max_bytes=len(data))
    else:
        tool_context.execution_client = SimpleNamespace(
            agent_id=uuid4(), current_directory="/work", read_bytes=AsyncMock(return_value=data),
        )
    if name == "edit_image":
        ai, chat = codex_edit_settings()
        tool_context.model_id = chat.id
        provider = AsyncMock(return_value=MediaResult(data, "image/png", "edited.png"))
        monkeypatch.setattr(MediaService, "edit_image", provider)
    else:
        ai = media_settings(ModelCapability.AUDIO_TRANSCRIPTION)
        provider = AsyncMock(return_value=SimpleNamespace(text="transcribed"))
        monkeypatch.setattr(MediaService, "transcribe_audio", provider)
    mock_settings_load(monkeypatch, ToolSettings(private_permission_preset="ask_all", public_permission_preset="ask_all"), ai)

    result = await tool_registry.execute(call, tool_context)

    assert not result.is_error, result.content
    if name == "read_qr_code":
        assert json.loads(result.content) == {"codes": ["file input"]}
    elif name == "transcribe_audio":
        assert provider.await_args.kwargs["audio"] == data
    else:
        assert provider.await_args.kwargs["images"] == ((data, "image/png"),)
        scope = json.loads(tool_context.ask_handler.await_args.args[0].resource_scope)
        assert scope["sourceFiles"] == result.attachments[0].metadata["sourceFiles"]


@pytest.mark.parametrize("failure,code", [
    ("denied", "permission.denied"),
    ("invalid", "attachment.unsupportedType"),
    ("oversized", "file.tooLarge"),
])
async def test_media_file_read_enforces_permission_validation_and_limit(tmp_path, monkeypatch, failure, code):
    import mato_loop_backend.tools.media as media

    call = ToolCall("qr", "read_qr_code", {"root": "computer", "path": "qr.png"})
    tool_context = context(tmp_path, call, PermissionDecision.DENY if failure == "denied" else PermissionDecision.ALLOW)
    client = SimpleNamespace(agent_id=uuid4(), current_directory="/work", read_bytes=AsyncMock(return_value=b"invalid"))
    tool_context.execution_client = client
    if failure == "oversized":
        monkeypatch.setattr(media, "MAX_IMAGE_ATTACHMENT_BYTES", 1)

    result = await tool_registry.execute(call, tool_context)

    assert result.metadata == {"errorCode": code}
    if failure == "denied":
        client.read_bytes.assert_not_awaited()
