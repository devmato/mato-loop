import base64
import json
import re
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.context import ContextManager
from mato_loop_backend.files.service import RootFileService
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.media.attachments import AttachmentService, validate_image
from mato_loop_backend.models import ChatSession, Message
from mato_loop_backend.providers import lemonade, token_counting
from mato_loop_backend.providers import service as service_module
from mato_loop_backend.providers.base import (
    ProviderError,
    ProviderImageInput,
    ProviderMedia,
    ProviderMediaInput,
    ProviderRequest,
    ProviderResult,
    PublicReasoning,
    ToolCall,
)
from mato_loop_backend.providers.openai_compatible import (
    _chat_body,
    _responses_body,
    discover_models,
)
from mato_loop_backend.providers.output_budget import (
    allocate_output_tokens,
    estimate_request_input_tokens,
    generation_reserve,
    guarded_input_tokens,
    output_token_limit,
    raw_input_budget,
)
from mato_loop_backend.providers.service import ProviderService, _with_retries
from mato_loop_backend.settings.models import (
    AgentRuntimeSettings,
    LlmProviderConfig,
    ModelCapability,
    ModelConfig,
    ModelInputModality,
    ReasoningEffort,
    TrustLevel,
)

pytestmark = pytest.mark.usefixtures("mock_db_settings")


@pytest.mark.parametrize("outcome", ["supported", "unsupported", "timeout", "invalid"])
async def test_server_token_count_uses_protected_request_and_caches_failures(monkeypatch, outcome):
    provider = LlmProviderConfig(
        name="Local", adapter_type="openai-compatible", config={"base_url": "http://local.test/v1"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="model-alias")
    prepared = await ProviderService.prepare_request(
        provider=provider, model=model, messages=[{"role": "user", "content": "anna@example.com"}],
        tools=[{"name": "read", "parameters": {"type": "object"}}],
        pseudonymization_enabled=True, pseudonymization_key=b"r" * 32,
    )
    requests = []

    def respond(request):
        requests.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("Unavailable")
        return httpx.Response(404 if outcome == "unsupported" else 200, json={"count": True if outcome == "invalid" else 123})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(token_counting.httpx, "AsyncClient", lambda **kwargs: client)
    first = await ProviderService.count_input_tokens(AsyncMock(spec=AsyncSession), provider=provider, prepared=prepared)
    if outcome == "supported":
        assert (first, str(requests[0].url), json.loads(requests[0].content)) == (
            123, "http://local.test/tokenize",
            {key: _chat_body(prepared.request)[key] for key in ("model", "messages", "tools")},
        )
    else:
        second = await ProviderService.count_input_tokens(AsyncMock(spec=AsyncSession), provider=provider, prepared=prepared)
        assert (first, second, len(requests)) == (None, None, 1)


async def test_stale_server_count_cannot_bypass_final_budget():
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible", pseudonymization_enabled=False)
    model = ModelConfig(provider_id=provider.id, model_id="test", context_window_tokens=2_048, max_output_tokens=100)
    prepared = await ProviderService.prepare_request(
        provider=provider, model=model, messages=[{"role": "user", "content": "Hello"}], tools=[],
    )
    with pytest.raises(ProviderError) as error:
        await ProviderService.complete(
            AsyncMock(spec=AsyncSession), provider=provider, model=model, tools=[],
            messages=[{"role": "user", "content": "x" * 10_000}], estimated_input_tokens=100,
            counted_input_fingerprint=prepared.input_token_fingerprint, safety_reserve_tokens=0,
            prepared_request=prepared,
        )
    assert error.value.code == "agent.contextWindowExceeded"


def _png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


async def test_prepared_request_revalidates_an_in_place_trust_change():
    provider = LlmProviderConfig(name="Test", adapter_type="openai-compatible", trust_level=TrustLevel.PRIVATE,
                                 pseudonymization_enabled=False)
    model = ModelConfig(provider_id=provider.id, model_id="test")
    options = dict(provider=provider, model=model, messages=[{"role": "user", "content": "Hello"}], tools=[])
    prepared = await ProviderService.prepare_request(**options)
    provider.trust_level = TrustLevel.PUBLIC
    public = await ProviderService.prepare_request(**options, prepared_request=prepared)
    assert public.request.continuation_scope["trustLevel"] == "public"


def _attachment_reference(
    session_id,
    path: str | None,
    data: bytes,
    *,
    attachment_id=None,
    filename: str = "image.png",
    category: str = "user_upload",
    trust_level: TrustLevel = TrustLevel.PUBLIC,
    file_root_id=None,
    file_id=None,
    file_revision: int | None = None,
    file_path: str | None = None,
) -> dict:
    validated = validate_image(data, "image/png")
    attachment_id = attachment_id or uuid4()
    if path is not None and file_root_id is None:
        file_root_id = session_id
        file_id = attachment_id
        file_revision = 1
        file_path = path
    return {
        "id": str(attachment_id),
        "filename": filename,
        "kind": "image",
        "category": category,
        "mime_type": validated.mime_type,
        "size_bytes": validated.size_bytes,
        "sha256": validated.sha256,
        "trust_level": trust_level.value,
        "session_id": str(session_id),
        "file_root_id": str(file_root_id) if file_root_id else None,
        "file_id": str(file_id) if file_id else None,
        "file_revision": file_revision,
        "file_path": file_path,
    }


@pytest.mark.parametrize("replay", [{"chat_reasoning_details": [{"data": "opaque"}]}, {"chat_reasoning_content": "Explicit reasoning"}])
async def test_complete_passes_model_capabilities_and_explicit_timeout(monkeypatch, replay) -> None:
    provider = LlmProviderConfig(
        name="Local",
        adapter_type="openai-compatible",
        pseudonymization_enabled=False,
        config={"base_url": "http://local.test/v1"},
    )
    model = ModelConfig(
        provider_id=provider.id,
        model_id="local-model",
        config={
            "max_output_tokens": 768,
            "supports_parallel_tool_calls": True,
            "supports_streaming": False,
            "temperature": 0.4,
        },
    )
    captured = {}

    async def fake_chat_completions(**kwargs):
        captured.update(kwargs)
        return ProviderResult(
            content="OK",
            provider_data=dict(replay),
        )

    monkeypatch.setattr(service_module, "chat_completions", fake_chat_completions)

    result = await ProviderService.complete(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        messages=[{"role": "user", "content": "Test"}],
        tools=[{"name": "read_file", "parameters": {}}],
        tool_choice="required",
        timeout=12,
        prompt_cache_key="ignored-for-chat-completions",
    )

    request = captured["request"]
    assert result.content == "OK"
    assert request.max_output_tokens == 768
    assert request.supports_parallel_tool_calls is True
    assert request.stream is False
    assert request.temperature == 0.4
    assert request.timeout == 12
    assert request.tool_choice == "required"
    assert request.prompt_cache_key is None
    assert result.provider_data["continuation_scope"]["providerId"] == str(provider.id)


@pytest.mark.parametrize(
    ("adapter_type", "metadata", "projected", "include_usage"),
    [
        ("openai-compatible", {"owned_by": "llamacpp"}, True, True),
        ("lemonade", {"recipe": "llamacpp"}, True, True),
        ("openai-compatible", {"owned_by": "vendor"}, False, True),
        ("lemonade", {"recipe": "flm"}, False, True),
    ],
)
async def test_complete_relaxes_only_llamacpp_repetition_limits(
    monkeypatch, adapter_type, metadata, projected, include_usage
) -> None:
    provider = LlmProviderConfig(
        name="Local",
        adapter_type=adapter_type,
        pseudonymization_enabled=False,
        config={"base_url": "http://local.test/v1"},
    )
    model = ModelConfig(
        provider_id=provider.id,
        model_id="local-model",
        discovery_metadata=metadata,
    )
    tools = [
        {
            "name": "complex_tool",
            "parameters": {
                "$defs": {
                    "Item": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2_000,
                    }
                },
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {"$ref": "#/$defs/Item"},
            },
        }
    ]
    transport = AsyncMock(return_value=ProviderResult(content="OK"))
    monkeypatch.setattr(service_module, "chat_completions", transport)

    result = await ProviderService.complete(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        messages=[{"role": "user", "content": "Test"}],
        tools=tools,
    )

    request = transport.await_args.kwargs["request"]
    sent = request.tools
    assert (sent is not tools) is projected
    assert ("maxItems" not in sent[0]["parameters"]) is projected
    assert ("maxLength" not in sent[0]["parameters"]["$defs"]["Item"]) is projected
    assert sent[0]["parameters"]["minItems"] == 1
    assert sent[0]["parameters"]["items"] == {"$ref": "#/$defs/Item"}
    assert tools[0]["parameters"]["maxItems"] == 50
    assert tools[0]["parameters"]["$defs"]["Item"]["maxLength"] == 2_000
    assert request.include_usage is include_usage
    assert result.provider_data["output_budget"]["finalEstimatedInputTokens"] == (
        estimate_request_input_tokens(_chat_body(request), model.characters_per_token)
    )


async def test_complete_validates_and_transports_direct_audio_and_video(monkeypatch) -> None:
    provider = LlmProviderConfig(
        name="vLLM",
        adapter_type="openai-compatible",
        pseudonymization_enabled=False,
        config={"base_url": "http://vllm.test/v1", "api_type": "chat_completions"},
    )
    model = ModelConfig(
        provider_id=provider.id,
        model_id="multimodal",
        input_modalities={
            ModelInputModality.TEXT,
            ModelInputModality.AUDIO,
            ModelInputModality.VIDEO,
        },
    )
    captured = {}

    async def fake_chat_completions(**kwargs):
        captured.update(kwargs)
        return ProviderResult(content="OK")

    monkeypatch.setattr(service_module, "chat_completions", fake_chat_completions)
    inputs = [
        ProviderMediaInput(0, b"RIFFaudio", "audio/wav", "private", "audio"),
        ProviderMediaInput(0, b"video", "video/mp4", "private", "video"),
    ]

    await ProviderService.complete(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        messages=[{"role": "user", "content": "Test"}],
        tools=[],
        media_inputs=inputs,
    )

    media = captured["request"].media
    assert base64.b64decode(media[0].data_base64) == b"RIFFaudio"
    assert base64.b64decode(media[1].data_url.split(",", 1)[1]) == b"video"
    assert "RIFFaudio" not in repr(inputs)
    assert "UklGRmF1ZGlv" not in repr(media)


@pytest.mark.parametrize(
    ("adapter_type", "api_type"),
    [
        ("openai-compatible", "responses"),
        ("openai-codex", "chat_completions"),
        ("lemonade", "chat_completions"),
    ],
)
async def test_complete_rejects_native_media_outside_openai_chat_completions(
    adapter_type: str, api_type: str
) -> None:
    provider = LlmProviderConfig(
        name="Unsupported",
        adapter_type=adapter_type,
        config={"base_url": "http://provider.test/v1", "api_type": api_type},
    )
    model = ModelConfig(
        provider_id=provider.id,
        model_id="audio",
        input_modalities={"text", "audio"},
    )

    with pytest.raises(ProviderError) as exc_info:
        await ProviderService.complete(
            AsyncMock(spec=AsyncSession),
            provider=provider,
            model=model,
            messages=[{"role": "user", "content": "Test"}],
            tools=[],
            media_inputs=[
                ProviderMediaInput(0, b"audio", "audio/wav", "public", "audio")
            ],
        )

    assert exc_info.value.public_code == "provider.modelCapabilityUnavailable"


async def test_complete_rejects_unconfigured_or_oversized_native_media(
    monkeypatch,
) -> None:
    provider = LlmProviderConfig(
        name="Public",
        adapter_type="openai-compatible",
        config={"base_url": "http://provider.test/v1"},
    )
    text_model = ModelConfig(provider_id=provider.id, model_id="text")
    audio_model = ModelConfig(
        provider_id=provider.id,
        model_id="audio",
        input_modalities={"text", "audio"},
    )
    media = ProviderMediaInput(0, b"audio", "audio/wav", "public", "audio")

    with pytest.raises(ProviderError, match="does not support"):
        await ProviderService.complete(
            AsyncMock(spec=AsyncSession),
            provider=provider,
            model=text_model,
            messages=[{"role": "user", "content": "Test"}],
            tools=[],
            media_inputs=[media],
        )
    monkeypatch.setattr(service_module, "MAX_PROVIDER_AUDIO_BYTES", 4)
    with pytest.raises(ProviderError) as exc_info:
        await ProviderService.complete(
            AsyncMock(spec=AsyncSession),
            provider=provider,
            model=audio_model,
            messages=[{"role": "user", "content": "Test"}],
            tools=[],
            media_inputs=[media],
        )

    assert exc_info.value.public_code == "provider.responseTooLarge"


@pytest.mark.parametrize("api_type", ["chat_completions", "responses"])
async def test_public_complete_pseudonymizes_and_restores_request_scope(
    monkeypatch, api_type,
) -> None:
    provider = LlmProviderConfig(
        name="Public",
        adapter_type="openai-compatible",
        config={"base_url": "http://public.test/v1", "api_type": api_type},
    )
    model = ModelConfig(provider_id=provider.id, model_id="public-model")
    streamed: list[str] = []
    reasoned: list[tuple[str, str]] = []

    async def fake_chat_completions(**kwargs):
        request = kwargs["request"]
        serializer = _responses_body if api_type == "responses" else _chat_body
        assert estimate_request_input_tokens(serializer(request)) == prepared.estimated_input_tokens
        assert request.messages != prepared.request.messages
        protected = request.messages[-1]["content"]
        assert "anna@example.com" not in protected
        assert "provider_data" not in request.messages[-1]
        assert request.messages[0]["role"] == "system"
        assert "Host privacy placeholders" in request.messages[0]["content"]
        markers = re.findall(r"\[\[MLP_[A-F0-9]{32}\]\]", protected)
        assert markers
        pseudonym = markers[-1]
        await kwargs["on_delta"]("Kontakt: " + pseudonym[:7])
        await kwargs["on_delta"](pseudonym[7:])
        await kwargs["on_reasoning"]("Pruefe " + pseudonym, "summary")
        return ProviderResult(
            content="Antwort an " + pseudonym,
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="send_mail",
                    arguments={"address": pseudonym, "nested": [pseudonym]},
                )
            ],
            provider_data={
                "responses_output_items": [{"id": "opaque"}],
                "reasoning_encrypted_content": "opaque",
                "chat_reasoning_details": [{"data": "opaque"}],
                "chat_reasoning_content": "Private reasoning",
            },
            reasoning=PublicReasoning(text="Gedanke zu " + pseudonym),
        )

    monkeypatch.setattr(service_module, "chat_completions", fake_chat_completions)
    monkeypatch.setattr(service_module, "responses_api", fake_chat_completions)
    messages = [{
        "role": "user",
        "content": "Schreibe an anna@example.com",
        "provider_data": {"responses_output_items": [{"id": "previous"}]},
    }]
    original = deepcopy(messages)
    prepared = await ProviderService.prepare_request(
        provider=provider, model=model, messages=messages, tools=[],
    )
    unprotected = await ProviderService.prepare_request(
        provider=provider, model=model, messages=messages, tools=[],
        pseudonymization_enabled=False,
    )
    assert prepared.estimated_input_tokens > unprotected.estimated_input_tokens

    result = await ProviderService.complete(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        messages=messages,
        tools=[],
        on_delta=lambda text: _append_async(streamed, text),
        on_reasoning=lambda text, kind: _append_async(reasoned, (text, kind)),
    )

    assert "".join(streamed) == "Kontakt: anna@example.com"
    assert "".join(text for text, _kind in reasoned) == "Pruefe anna@example.com"
    assert result.content == "Antwort an anna@example.com"
    assert result.tool_calls[0].arguments == {
        "address": "anna@example.com",
        "nested": ["anna@example.com"],
    }
    assert result.reasoning is not None
    assert result.reasoning.text == "Gedanke zu anna@example.com"
    assert set(result.provider_data) == {"output_budget"}
    assert messages == original
    assert result.provider_data["output_budget"]["finalEstimatedInputTokens"] == prepared.estimated_input_tokens


@pytest.mark.parametrize("api_type", ["chat_completions", "responses"])
@pytest.mark.parametrize("trust_level", ["public", "private"])
async def test_run_privacy_keeps_serialized_prefix_and_request_scope(monkeypatch, api_type, trust_level) -> None:
    provider = LlmProviderConfig(
        name="Public", adapter_type="openai-compatible",
        trust_level=trust_level,
        config={"base_url": "http://public.test/v1", "api_type": api_type},
    )
    model = ModelConfig(provider_id=provider.id, model_id="public-model")
    options = dict(provider=provider, model=model, tools=[], pseudonymization_key=b"r" * 32,
                   pseudonymization_cache=service_module.ProtectionCache())
    source = [{"role": "user", "content": "Contact anna@example.com"}]
    first = await ProviderService.prepare_request(messages=source, **options)
    # Candidate preparation neither changes identities nor grants later restoration access.
    await ProviderService.prepare_request(
        messages=[{"role": "user", "content": "discarded@example.com"}], **options,
    )
    second = await ProviderService.prepare_request(
        messages=[*source, {"role": "assistant", "content": "Noted"},
                  {"role": "user", "content": "Also bob@example.com"}], **options,
    )
    serializer = _responses_body if api_type == "responses" else _chat_body
    field = "input" if api_type == "responses" else "messages"
    before, after = serializer(first.request), serializer(second.request)
    assert after[field][:len(before[field])] == before[field]
    if api_type == "responses":
        assert before["instructions"] == after["instructions"]
    old_marker = first.request.messages[-1]["content"].removeprefix("Contact ")

    async def response(**kwargs):
        assert kwargs["request"].messages == first.request.messages
        return ProviderResult(content=old_marker)

    monkeypatch.setattr(service_module, "chat_completions", response)
    monkeypatch.setattr(service_module, "responses_api", response)
    projection = AsyncMock(wraps=service_module._provider_projection)
    monkeypatch.setattr(service_module, "_provider_projection", projection)
    result = await ProviderService.complete(
        AsyncMock(spec=AsyncSession), messages=source, prepared_request=first, **options,
    )
    assert projection.await_count == 0
    assert result.content == "anna@example.com"

    current = await ProviderService.prepare_request(
        messages=[{"role": "user", "content": old_marker}], **options,
    )
    assert current.pseudonymizer is not None
    with pytest.raises(service_module.PseudonymizationError):
        current.pseudonymizer.restore_value({"recipient": old_marker})


async def test_privacy_drops_authority_for_serializer_omitted_fields() -> None:
    provider = LlmProviderConfig(
        name="Public", adapter_type="openai-compatible",
        config={"base_url": "http://public.test/v1", "api_type": "responses"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="public-model")
    key = b"r" * 32
    marker = service_module.RequestPseudonymizer(key=key).protect_text("omitted@example.com")
    prepared = await ProviderService.prepare_request(
        provider=provider, model=model, tools=[], pseudonymization_key=key, messages=[{
            "role": "system", "content": "System",
            "tool_calls": [{"id": "omitted", "name": "read", "arguments": {
                "contact": "omitted@example.com",
            }}],
        }, {"role": "user", "content": marker}],
    )
    assert "tool_calls" not in prepared.request.messages[0]
    assert prepared.pseudonymizer is not None
    with pytest.raises(service_module.PseudonymizationError):
        prepared.pseudonymizer.restore_value({"recipient": marker})


async def test_public_complete_rejects_unknown_pseudonym_marker(monkeypatch) -> None:
    provider = LlmProviderConfig(
        name="Public",
        adapter_type="openai-compatible",
        config={"base_url": "http://public.test/v1"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="public-model")

    async def fake_chat_completions(**kwargs):
        protected = kwargs["request"].messages[-1]["content"]
        marker = re.search(r"\[\[MLP_[A-F0-9]{32}\]\]", protected)
        assert marker is not None
        unknown_marker = marker.group()[:-2] + "UNKNOWN]]"
        return ProviderResult(
            content="",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="send_mail",
                    arguments={"address": unknown_marker},
                )
            ],
        )

    monkeypatch.setattr(service_module, "chat_completions", fake_chat_completions)

    with pytest.raises(ProviderError) as exc_info:
        await ProviderService.complete(
            AsyncMock(spec=AsyncSession),
            provider=provider,
            model=model,
            messages=[{"role": "user", "content": "Schreibe an anna@example.com"}],
            tools=[],
        )

    assert exc_info.value.code == "privacy.pseudonymizationFailed"


async def test_public_complete_warns_and_continues_for_unknown_visible_marker(
    monkeypatch,
) -> None:
    provider = LlmProviderConfig(
        name="Public",
        adapter_type="openai-compatible",
        config={"base_url": "http://public.test/v1"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="public-model")
    streamed = []
    reasoned = []
    warning = AsyncMock()

    async def fake_chat_completions(**kwargs):
        protected = kwargs["request"].messages[-1]["content"]
        marker = re.search(r"\[\[MLP_[A-F0-9]{32}\]\]", protected)
        assert marker is not None
        unknown = marker.group()[:-2] + "UNKNOWN]]"
        await kwargs["on_delta"]("Kontakt: " + unknown)
        await kwargs["on_reasoning"]("Pruefe " + unknown, "summary")
        return ProviderResult(
            content="Antwort an " + unknown,
            reasoning=PublicReasoning(text="Gedanke zu " + unknown),
        )

    monkeypatch.setattr(service_module, "chat_completions", fake_chat_completions)

    result = await ProviderService.complete(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        messages=[{"role": "user", "content": "Schreibe an anna@example.com"}],
        tools=[],
        on_delta=lambda text: _append_async(streamed, text),
        on_reasoning=lambda text, kind: _append_async(reasoned, (text, kind)),
        on_warning=warning,
    )

    assert "".join(streamed) == "Kontakt: [...]"
    assert reasoned == [("Pruefe [...]", "summary")]
    assert result.content == "Antwort an [...]"
    assert result.reasoning is not None
    assert result.reasoning.text == "Gedanke zu [...]"
    warning.assert_awaited_once_with("privacy.placeholderRestorationIncomplete")


async def _append_async(items: list, value) -> None:
    items.append(value)


async def test_codex_complete_uses_subscription_compatible_responses_payload(monkeypatch) -> None:
    credential_id = uuid4()
    credential = SimpleNamespace(user_id=None)
    provider = LlmProviderConfig(
        name="Codex",
        adapter_type="openai-codex",
        credential_id=credential_id,
    )
    model = ModelConfig(
        provider_id=provider.id,
        model_id="openai-codex/gpt-5.6-sol",
        supports_parallel_tool_calls=True,
        discovery_metadata={
            "default_reasoning_effort": "low",
            "reasoning_efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
        },
        config={"temperature": 0.5},
    )
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = credential
    payload = {
        "access": "access",
        "refresh": "refresh",
        "account_id": "account",
    }
    captured = {}

    async def fake_responses_api(**kwargs):
        captured.update(kwargs)
        captured["body"] = _responses_body(
            kwargs["request"], kwargs.get("extra_body")
        )
        return ProviderResult(content="OK")

    monkeypatch.setattr(
        service_module,
        "refresh_codex_credential",
        AsyncMock(return_value=payload),
    )
    monkeypatch.setattr(service_module, "responses_api", fake_responses_api)

    result = await ProviderService.complete(
        db,
        provider=provider,
        model=model,
        messages=[{"role": "user", "content": "Test"}],
        tools=[{"name": "read_file", "parameters": {}}],
        max_output_tokens=1_024,
        prompt_cache_key="session-1",
    )

    request = captured["request"]
    assert result.content == "OK"
    assert request.model == "gpt-5.6-sol"
    assert request.max_output_tokens is None
    budget = result.provider_data["output_budget"]
    assert budget["allocatedOutputTokens"] == 1_024
    assert budget["transmittedMaxOutputTokens"] is None
    assert request.temperature is None
    assert request.tools == [{"name": "read_file", "parameters": {}}]
    assert request.supports_parallel_tool_calls is True
    assert request.prompt_cache_key == "session-1"
    assert "extra_body" not in captured
    assert "max_output_tokens" not in captured["body"]
    assert "temperature" not in captured["body"]
    assert "text" not in captured["body"]
    assert captured["body"]["prompt_cache_key"] == "session-1"
    assert captured["body"]["reasoning"]["effort"] == "low"

    await ProviderService.complete(
        db,
        provider=provider,
        model=model,
        messages=[{"role": "user", "content": "Test"}],
        tools=[],
        reasoning_effort="ultra",
    )
    assert captured["body"]["reasoning"]["effort"] == "ultra"


@pytest.mark.parametrize("role", ["user", "tool"])
async def test_complete_marks_images_without_opening_storage_for_text_models(
    monkeypatch, role,
) -> None:
    provider = LlmProviderConfig(
        name="Local",
        adapter_type="openai-compatible",
        config={"base_url": "http://local.test/v1"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="text-only", supports_image_input=False)
    captured = {}

    def fail_storage():
        raise AssertionError("AttachmentService must not be opened")

    async def fake_chat_completions(**kwargs):
        captured.update(kwargs)
        return ProviderResult(content="OK")

    monkeypatch.setattr(service_module, "AttachmentService", fail_storage)
    monkeypatch.setattr(service_module, "chat_completions", fake_chat_completions)

    await ProviderService.complete(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        messages=[
            {
                "role": role,
                "content": "Screenshot",
                "tool_call_id": "call-1",
                "attachments": [
                    _attachment_reference(
                        uuid4(),
                        "attachments/00000000-0000-4000-8000-000000000000/missing.png",
                        _png_bytes(),
                        category="user_upload",
                    )
                ],
            }
        ],
        tools=[],
    )

    request = captured["request"]
    assert "attachments" not in request.messages[-1]
    assert request.media == []
    assert request.messages[-1]["content"] == (
        "Screenshot\n\n[Image content unavailable to this model. Only image metadata is available.]"
    )


async def test_complete_materializes_valid_image_without_leaking_storage_key(
    monkeypatch, tmp_path: Path
) -> None:
    provider = LlmProviderConfig(
        name="Local",
        adapter_type="openai-compatible",
        config={"base_url": "http://local.test/v1"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="vision", supports_image_input=True)
    session_id = uuid4()
    sessions = SessionStorage(tmp_path / "sessions")
    sessions.provision(session_id)
    data = _png_bytes()
    root_files = RootFileService(session_id, session_storage=sessions)
    revision = root_files.create_output_file("image.png", data)
    attachment = _attachment_reference(
        session_id,
        None,
        data,
        category="user_upload",
        file_root_id=session_id,
        file_id=revision.file_id,
        file_revision=revision.revision,
        file_path=revision.path,
    )
    captured = {}
    async def fake_chat_completions(**kwargs):
        captured.update(kwargs)
        return ProviderResult(content="OK")

    monkeypatch.setattr(
        service_module, "AttachmentService", lambda: AttachmentService(sessions)
    )
    monkeypatch.setattr(service_module, "chat_completions", fake_chat_completions)

    await ProviderService.complete(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        messages=[
            {
                "role": "tool",
                "content": "Screenshot",
                "tool_call_id": "call-1",
                "attachments": [attachment],
            }
        ],
        tools=[],
    )

    request = captured["request"]
    assert request.media[0].status == "ready"
    assert request.media[0].data_url is not None
    assert base64.b64decode(request.media[0].data_url.split(",", 1)[1]) == data
    assert revision.path not in repr(request.messages)
    assert revision.path not in repr(request.media)


async def test_projection_rejects_stale_image_without_substituting_current_bytes(
    monkeypatch, tmp_path: Path
) -> None:
    session_id = uuid4()
    sessions = SessionStorage(tmp_path / "sessions")
    sessions.provision(session_id)
    root_files = RootFileService(session_id, session_storage=sessions)
    data = _png_bytes()
    revision = root_files.create_output_file("image.png", data)
    newer_buffer = BytesIO()
    Image.new("RGB", (2, 2), (90, 80, 70)).save(newer_buffer, format="PNG")
    newer_data = newer_buffer.getvalue()
    root_files.append_output_revision(revision.file_id, newer_data)
    reference = _attachment_reference(
        session_id,
        None,
        data,
        category="generated",
        file_root_id=session_id,
        file_id=revision.file_id,
        file_revision=revision.revision,
        file_path=revision.path,
    )
    monkeypatch.setattr(
        service_module, "AttachmentService", lambda: AttachmentService(sessions)
    )

    _projected, media = await service_module._provider_projection(
        [{"role": "tool", "content": "Screenshot", "attachments": [reference]}],
        supports_image_input=True,
        target_trust_level=TrustLevel.PUBLIC,
    )

    assert media[0].status == "unavailable"
    assert media[0].data_url is None


async def test_complete_uses_unavailable_marker_for_missing_attachment_file(
    monkeypatch, tmp_path: Path
) -> None:
    provider = LlmProviderConfig(
        name="Local",
        adapter_type="openai-compatible",
        config={"base_url": "http://local.test/v1"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="vision", supports_image_input=True)
    session_id = uuid4()
    sessions = SessionStorage(tmp_path / "sessions")
    sessions.provision(session_id)
    data = _png_bytes()
    captured = {}

    async def fake_chat_completions(**kwargs):
        captured.update(kwargs)
        return ProviderResult(content="OK")

    monkeypatch.setattr(
        service_module, "AttachmentService", lambda: AttachmentService(sessions)
    )
    monkeypatch.setattr(service_module, "chat_completions", fake_chat_completions)

    await ProviderService.complete(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        messages=[
            {
                "role": "tool",
                "content": "Screenshot",
                "tool_call_id": "call-1",
                "attachments": [
                    _attachment_reference(
                        session_id,
                        "attachments/00000000-0000-4000-8000-000000000000/missing.png",
                        data,
                        category="user_upload",
                    )
                ],
            }
        ],
        tools=[],
    )

    request = captured["request"]
    assert request.media[0].status == "unavailable"
    assert request.media[0].data_url is None


async def test_projection_sends_current_user_images_but_not_unreferenced_history(
    monkeypatch, tmp_path: Path
) -> None:
    session_id = uuid4()
    sessions = SessionStorage(tmp_path / "sessions")
    sessions.provision(session_id)
    root_files = RootFileService(session_id, session_storage=sessions)
    old_data = _png_bytes()
    current_buffer = BytesIO()
    Image.new("RGB", (2, 2), (90, 80, 70)).save(current_buffer, format="PNG")
    current_data = current_buffer.getvalue()
    old_revision = root_files.create_output_file("old.png", old_data)
    current_revision = root_files.create_output_file("current.png", current_data)
    monkeypatch.setattr(
        service_module, "AttachmentService", lambda: AttachmentService(sessions)
    )

    projected, media = await service_module._provider_projection(
        [
            {
                "role": "user",
                "content": "Alte Nachricht",
                "attachments": [
                    _attachment_reference(
                        session_id,
                        None,
                        old_data,
                        filename="old.png",
                        file_root_id=session_id,
                        file_id=old_revision.file_id,
                        file_revision=1,
                        file_path=old_revision.path,
                    )
                ],
            },
            {
                "role": "user",
                "content": "Aktuelles Bild",
                "attachments": [
                    _attachment_reference(
                        session_id,
                        None,
                        current_data,
                        filename="current.png",
                        file_root_id=session_id,
                        file_id=current_revision.file_id,
                        file_revision=1,
                        file_path=current_revision.path,
                    )
                ],
            },
        ],
        supports_image_input=True,
        target_trust_level=TrustLevel.PUBLIC,
    )

    assert all("attachments" not in message for message in projected)
    assert len(media) == 1
    assert media[0].message_index == 1
    assert base64.b64decode(media[0].data_url.split(",", 1)[1]) == current_data


async def test_projection_accepts_bounded_transient_images() -> None:
    data = _png_bytes()

    projected, media = await service_module._provider_projection(
        [{"role": "user", "content": "PDF page 1"}],
        supports_image_input=True,
        target_trust_level=TrustLevel.PUBLIC,
        image_inputs=[
            ProviderImageInput(
                0, data, trust_level=TrustLevel.PUBLIC.value, mime_type="image/png"
            )
        ],
    )

    assert projected == [{"role": "user", "content": "PDF page 1"}]
    assert len(media) == 1
    assert media[0].message_index == 0
    assert base64.b64decode(media[0].data_url.split(",", 1)[1]) == data

    _projected, private_media = await service_module._provider_projection(
        [{"role": "user", "content": "PDF page 1"}],
        supports_image_input=True,
        target_trust_level=TrustLevel.PUBLIC,
        image_inputs=[
            ProviderImageInput(
                0, data, trust_level=TrustLevel.PRIVATE.value, mime_type="image/png"
            )
        ],
    )
    assert len(private_media) == 1

    with pytest.raises(ProviderError) as exc_info:
        await service_module._provider_projection(
            [{"role": "user", "content": "Frames"}],
            supports_image_input=True,
            target_trust_level=TrustLevel.PUBLIC,
            image_inputs=[
                ProviderImageInput(0, data, TrustLevel.PUBLIC.value)
                for _ in range(service_module.MAX_PROVIDER_IMAGES + 1)
            ],
        )
    assert exc_info.value.public_code == "provider.imageLimitExceeded"
    assert exc_info.value.params == {"maxImages": service_module.MAX_PROVIDER_IMAGES}


@pytest.mark.parametrize("api_type", ["chat_completions", "responses"])
async def test_projection_combines_persisted_and_direct_images_with_shared_limits(monkeypatch, api_type):
    data = _png_bytes()
    reference = _attachment_reference(uuid4(), "reference.png", data)
    data_url = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    monkeypatch.setattr(service_module, "_read_image_data_url", lambda _attachment: data_url)
    messages = [
        {"role": "user", "content": "Compare this reference.", "attachments": [reference]},
        {"role": "assistant", "content": "Ephemeral tool-result image; not saved."},
    ]
    original = deepcopy(messages)
    image = ProviderImageInput(1, data, "private", "image/png")
    provider = LlmProviderConfig(
        name="Test", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "https://llm.test/v1", "api_type": api_type},
    )
    model = ModelConfig(provider_id=provider.id, model_id="vision", supports_image_input=True)
    options = dict(provider=provider, model=model, messages=messages, tools=[], image_inputs=[image])
    prepared = await ProviderService.prepare_request(**options)
    assert [(item.message_index, item.data_url) for item in prepared.request.media] == [
        (0, data_url), (1, data_url),
    ]
    transport = AsyncMock(return_value=ProviderResult(content="Compared."))
    monkeypatch.setattr(service_module, "responses_api" if api_type == "responses" else "chat_completions", transport)
    result = await ProviderService.complete(
        AsyncMock(spec=AsyncSession), **options, safety_reserve_tokens=0,
        estimated_input_tokens=prepared.estimated_input_tokens,
    )
    assert transport.await_args.kwargs["request"].media == prepared.request.media
    assert result.provider_data["output_budget"]["finalEstimatedInputTokens"] == prepared.estimated_input_tokens
    assert messages == original
    with pytest.raises(ProviderError) as error:
        await ProviderService.prepare_request(**{**options, "image_inputs": [image] * 8})
    assert error.value.code == "provider.imageLimitExceeded"
    monkeypatch.setattr(service_module, "MAX_PROVIDER_IMAGE_BYTES", len(data) * 2 - 1)
    with pytest.raises(ProviderError) as error:
        await ProviderService.prepare_request(**options)
    assert error.value.code == "provider.responseTooLarge"
    monkeypatch.setattr(service_module, "MAX_PROVIDER_IMAGE_BYTES", len(data) * 2)
    with pytest.raises(ProviderError) as error:
        await ProviderService.prepare_request(**{
            **options, "image_inputs": [replace(image, trust_level="invalid")],
        })
    assert error.value.code == "provider.configurationInvalid"
    with pytest.raises(ProviderError) as error:
        await ProviderService.prepare_request(**{
            **options, "image_inputs": [replace(image, data=b"invalid")],
        })
    assert error.value.code == "provider.invalidResponse"
    text_only = await ProviderService.prepare_request(**{
        **options, "model": model.model_copy(update={"input_modalities": {ModelInputModality.TEXT}}),
    })
    assert text_only.request.media == []
    assert all(
        message["content"].endswith(
            "[Image content unavailable to this model. Only image metadata is available.]"
        )
        for message in text_only.request.messages
    )
    assert messages == original


async def test_projection_accepts_and_downsizes_mpo_images_for_provider() -> None:
    buffer = BytesIO()
    Image.new("RGB", (3000, 2000), (10, 20, 30)).save(
        buffer,
        format="MPO",
        save_all=True,
        append_images=[Image.new("RGB", (3, 2), (30, 20, 10))],
    )

    _projected, media = await service_module._provider_projection(
        [{"role": "user", "content": "Großes Bild"}],
        supports_image_input=True,
        target_trust_level=TrustLevel.PUBLIC,
        image_inputs=[
            ProviderImageInput(
                0,
                buffer.getvalue(),
                trust_level=TrustLevel.PUBLIC.value,
                mime_type="image/jpeg",
            )
        ],
    )

    normalized = base64.b64decode(media[0].data_url.split(",", 1)[1])
    with Image.open(BytesIO(normalized)) as image:
        assert image.format == "JPEG"
        assert image.size == (2048, 1365)
    assert len(normalized) <= service_module._MODEL_IMAGE_MAX_BYTES


@pytest.mark.parametrize("supports_image_input", [True, False])
async def test_session_image_read_reactivates_historical_image(monkeypatch, supports_image_input) -> None:
    attachment_id = uuid4()
    reference = _attachment_reference(
        uuid4(),
        f"attachments/{attachment_id}/scan.png",
        _png_bytes(),
        attachment_id=attachment_id,
        filename="scan.png",
    )
    reference["historical"] = True
    reference["source_order"] = 0
    monkeypatch.setattr(
        service_module,
        "_read_image_data_url",
        lambda attachment: f"data:image/png;base64,{attachment['id']}",
    )

    projected, media = await service_module._provider_projection(
        [
            {"role": "system", "content": "System", "attachments": [reference]},
            {"role": "user", "content": "Lies den alten Verlauf"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "read",
                        "name": "read",
                        "arguments": {
                            "root": "session",
                            "path": reference["file_path"],
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "read",
                "content": json.dumps(
                    {
                        "root": "session",
                        "path": reference["file_path"],
                        "imageAttachment": {
                            "id": str(attachment_id),
                            "filename": "scan.png",
                            "kind": "image",
                            "mimeType": "image/png",
                        },
                        "contentIsUntrusted": True,
                    }
                ),
            },
        ],
        supports_image_input=supports_image_input,
        target_trust_level=TrustLevel.PUBLIC,
    )

    marker = "[Image content unavailable to this model. Only image metadata is available.]"
    if supports_image_input:
        assert len(media) == 1
        assert media[0].message_index == 3
        assert media[0].status == "ready"
        assert marker not in projected[3]["content"]
    else:
        assert media == []
        assert projected[3]["content"].endswith(marker)
        assert all(marker not in message["content"] for message in projected[:3])


async def test_historical_image_limit_prefers_newest_references(monkeypatch) -> None:
    references = []
    for order in range(9):
        attachment_id = uuid4()
        reference = _attachment_reference(
            uuid4(),
            f"attachments/{attachment_id}/image-{order}.png",
            _png_bytes(),
            attachment_id=attachment_id,
            filename=f"image-{order}.png",
        )
        reference.update({"historical": True, "source_order": order})
        references.append(reference)
    monkeypatch.setattr(
        service_module,
        "_read_image_data_url",
        lambda attachment: f"data:image/png;base64,{attachment['id']}",
    )

    _projected, media = await service_module._provider_projection(
        [
            {"role": "system", "content": "System", "attachments": references},
            {
                "role": "user",
                "content": " ".join(reference["id"] for reference in references),
            },
        ],
        supports_image_input=True,
        target_trust_level=TrustLevel.PUBLIC,
    )

    assert len(media) == 8
    assert [item.data_url.rsplit(",", 1)[1] for item in media] == [
        reference["id"] for reference in references[1:]
    ]


async def test_required_contact_sheet_leaves_room_for_seven_current_references(
    monkeypatch,
) -> None:
    contact_sheet_session_id = uuid4()
    contact_sheet = _attachment_reference(
        contact_sheet_session_id,
        None,
        _png_bytes(),
        category="generated",
        file_root_id=contact_sheet_session_id,
        file_id=uuid4(),
        file_revision=1,
        file_path="model-views.png",
    )
    references = [
        _attachment_reference(uuid4(), f"attachments/reference-{index}.png", _png_bytes())
        for index in range(8)
    ]
    monkeypatch.setattr(
        service_module,
        "_read_image_data_url",
        lambda attachment: f"data:image/png;base64,{attachment['id']}",
    )

    _projected, media = await service_module._provider_projection(
        [
            {"role": "user", "content": "Use these references", "attachments": references},
            {
                "role": "tool",
                "content": "Rendered views",
                "tool_call_id": "render",
                "attachments": [contact_sheet],
            },
        ],
        supports_image_input=True,
        target_trust_level=TrustLevel.PUBLIC,
        required_image_ids=(contact_sheet["id"],),
    )

    assert [item.data_url.rsplit(",", 1)[1] for item in media] == [
        contact_sheet["id"],
        *(item["id"] for item in references[:7]),
    ]


async def test_public_target_materializes_authorized_private_image(monkeypatch) -> None:
    private_reference = _attachment_reference(
        uuid4(),
        f"attachments/{uuid4()}/private.png",
        _png_bytes(),
        trust_level=TrustLevel.PRIVATE,
    )
    private_reference["historical"] = True
    private_reference["source_order"] = 0

    read = Mock(return_value="data:image/png;base64,private")
    monkeypatch.setattr(service_module, "_read_image_data_url", read)

    _projected, media = await service_module._provider_projection(
        [
            {"role": "system", "content": "System", "attachments": [private_reference]},
            {"role": "user", "content": private_reference["id"]},
        ],
        supports_image_input=True,
        target_trust_level=TrustLevel.PUBLIC,
    )

    assert len(media) == 1
    assert media[0].status == "ready"
    assert media[0].data_url == "data:image/png;base64,private"
    read.assert_called_once_with(private_reference)


async def test_public_target_materializes_authorized_image_without_trust_label(monkeypatch) -> None:
    reference = _attachment_reference(
        uuid4(), f"attachments/{uuid4()}/unknown.png", _png_bytes()
    )
    reference.pop("trust_level")

    monkeypatch.setattr(
        service_module,
        "_read_image_data_url",
        Mock(return_value="data:image/png;base64,unknown"),
    )

    _projected, media = await service_module._provider_projection(
        [
            {"role": "system", "content": "System", "attachments": [reference]},
            {"role": "user", "content": reference["id"]},
        ],
        supports_image_input=True,
        target_trust_level=TrustLevel.PUBLIC,
    )

    assert media[0].status == "ready"
    assert media[0].data_url == "data:image/png;base64,unknown"


async def test_private_target_can_read_private_image(monkeypatch, tmp_path: Path) -> None:
    session_id = uuid4()
    sessions = SessionStorage(tmp_path / "sessions")
    sessions.provision(session_id)
    data = _png_bytes()
    revision = RootFileService(
        session_id, session_storage=sessions
    ).create_output_file("private.png", data)
    monkeypatch.setattr(
        service_module, "AttachmentService", lambda: AttachmentService(sessions)
    )

    _projected, media = await service_module._provider_projection(
        [
            {
                "role": "user",
                "content": "Privates Bild",
                "attachments": [
                    _attachment_reference(
                        session_id,
                        None,
                        data,
                        trust_level=TrustLevel.PRIVATE,
                        file_root_id=session_id,
                        file_id=revision.file_id,
                        file_revision=revision.revision,
                        file_path=revision.path,
                    )
                ],
            }
        ],
        supports_image_input=True,
        target_trust_level=TrustLevel.PRIVATE,
    )

    assert media[0].status == "ready"
    assert base64.b64decode(media[0].data_url.split(",", 1)[1]) == data


async def test_internal_file_path_never_reaches_openai_payload(monkeypatch) -> None:
    file_path = f"attachments/{uuid4()}/secret.png"
    reference = _attachment_reference(uuid4(), file_path, _png_bytes())
    monkeypatch.setattr(
        service_module,
        "_read_image_data_url",
        lambda _attachment: "data:image/png;base64,aW1hZ2U=",
    )

    projected, media = await service_module._provider_projection(
        [{"role": "user", "content": "Bild", "attachments": [reference]}],
        supports_image_input=True,
        target_trust_level=TrustLevel.PUBLIC,
    )
    request = service_module.ProviderRequest(
        model="vision", messages=projected, media=media
    )
    payload = json.dumps(
        {"chat": _chat_body(request), "responses": _responses_body(request)}
    )

    assert file_path not in payload
    assert "storage_path" not in payload
    assert "data:image/png" in payload


async def test_media_selection_precedes_pseudonymization(monkeypatch) -> None:
    provider = LlmProviderConfig(
        name="Public",
        adapter_type="openai-compatible",
        config={"base_url": "http://public.test/v1"},
    )
    model = ModelConfig(
        provider_id=provider.id,
        model_id="vision",
        supports_image_input=True,
    )
    attachment_id = uuid4()
    reference = _attachment_reference(
        uuid4(),
        f"attachments/{attachment_id}/anna@example.com.png",
        _png_bytes(),
        attachment_id=attachment_id,
        filename="anna@example.com.png",
    )
    reference.update({"historical": True, "source_order": 0})
    captured = {}

    class ReplacingPseudonymizer(service_module.RequestPseudonymizer):
        def protect_messages(self, messages):
            assert all("attachments" not in message for message in messages)
            return [
                {**message, "content": str(message.get("content") or "").replace("anna@example.com", "PERSON")}
                for message in messages
            ]

        def restore_text(self, text, **_kwargs):
            return text

        def restore_value(self, value):
            return value

    async def fake_chat_completions(**kwargs):
        captured.update(kwargs)
        return ProviderResult(content="OK")

    monkeypatch.setattr(service_module, "RequestPseudonymizer", ReplacingPseudonymizer)
    monkeypatch.setattr(
        service_module,
        "_read_image_data_url",
        lambda _attachment: "data:image/png;base64,aW1hZ2U=",
    )
    monkeypatch.setattr(service_module, "chat_completions", fake_chat_completions)

    await ProviderService.complete(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        messages=[
            {"role": "system", "content": "System", "attachments": [reference]},
            {"role": "user", "content": "Zeige anna@example.com.png"},
        ],
        tools=[],
        target_trust_level=TrustLevel.PUBLIC,
        pseudonymization_enabled=True,
    )

    request = captured["request"]
    assert request.media[0].status == "ready"
    assert "anna@example.com" not in request.messages[1]["content"]


async def test_transient_errors_use_retry_after(monkeypatch) -> None:
    operation = AsyncMock(
        side_effect=[
            ProviderError("rate limited", status_code=429, retry_after=1.5),
            ProviderResult(content="OK"),
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(service_module.asyncio, "sleep", sleep)

    result = await _with_retries(operation)

    assert result.content == "OK"
    assert operation.await_count == 2
    sleep.assert_awaited_once_with(1.5)


async def test_transient_errors_use_increasing_delays_and_report_attempts(
    monkeypatch,
) -> None:
    operation = AsyncMock(
        side_effect=[
            ProviderError("temporary", status_code=503),
            ProviderError("temporary", status_code=503),
            ProviderResult(content="OK"),
        ]
    )
    events = []
    sleep = AsyncMock(side_effect=lambda delay: events.append(("sleep", delay)))
    on_retry = AsyncMock(
        side_effect=lambda attempt, _total, _status, _code: events.append(
            ("retry", attempt)
        )
    )
    monkeypatch.setattr(service_module.asyncio, "sleep", sleep)

    result = await _with_retries(operation, on_retry=on_retry)

    assert result.content == "OK"
    assert operation.await_count == 3
    assert [item.args for item in sleep.await_args_list] == [(2.0,), (5.0,)]
    assert [item.args for item in on_retry.await_args_list] == [
        (2, 3, 503, "provider.unavailable"),
        (3, 3, 503, "provider.unavailable"),
    ]
    assert events == [("sleep", 2.0), ("retry", 2), ("sleep", 5.0), ("retry", 3)]


async def test_non_transient_error_is_not_retried() -> None:
    operation = AsyncMock(side_effect=ProviderError("bad request", status_code=400))

    with pytest.raises(ProviderError):
        await _with_retries(operation)

    operation.assert_awaited_once()


async def test_transient_error_is_not_retried_after_stream_output() -> None:
    operation = AsyncMock(side_effect=ProviderError("stream interrupted", status_code=503))

    with pytest.raises(ProviderError):
        await _with_retries(operation, can_retry=lambda: False)

    operation.assert_awaited_once()


@pytest.mark.parametrize("model_fields", [
    {},
    {"capabilities": {
        "chat": True, "tools": True, "reasoning": True,
        "image_generation": False, "reasoning_efforts": None,
    }},
])
async def test_discovery_gets_models_with_auth_and_preserves_opaque_ids(monkeypatch, model_fields) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://provider.test/v1/models"
        assert request.headers["Authorization"] == "Bearer secret-key"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "vendor/model:version@2026-07",
                        "object": "model",
                        "owned_by": "vendor",
                        **model_fields,
                    }
                ],
                "extra": True,
            },
        )

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "mato_loop_backend.providers.openai_compatible.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = await discover_models(
        base_url="https://provider.test/v1/",
        api_key="secret-key",
    )

    assert result[0].id == "vendor/model:version@2026-07"
    assert result[0].capabilities == {ModelCapability.CHAT}
    assert result[0].metadata == {"object": "model", "owned_by": "vendor"}


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [(401, "provider.authenticationFailed"), (404, "provider.endpointNotFound")],
)
async def test_discovery_maps_http_errors(
    monkeypatch, status_code: int, expected_code: str
) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: httpx.Response(status_code))
    monkeypatch.setattr(
        "mato_loop_backend.providers.openai_compatible.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    with pytest.raises(ProviderError) as exc_info:
        await discover_models(base_url="https://provider.test/v1", api_key=None)

    assert exc_info.value.public_code == expected_code


async def test_discovery_rejects_invalid_model_payload(monkeypatch) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"data": [{"name": "missing id"}]})
    )
    monkeypatch.setattr(
        "mato_loop_backend.providers.openai_compatible.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    with pytest.raises(ProviderError) as exc_info:
        await discover_models(base_url="https://provider.test/v1", api_key=None)

    assert exc_info.value.public_code == "provider.invalidResponse"


async def test_discovery_maps_network_errors(monkeypatch) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(fail)
    monkeypatch.setattr(
        "mato_loop_backend.providers.openai_compatible.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    with pytest.raises(ProviderError) as exc_info:
        await discover_models(base_url="https://provider.test/v1", api_key=None)

    assert exc_info.value.public_code == "provider.unavailable"


async def test_provider_discovery_uses_stored_api_key(monkeypatch) -> None:
    provider = LlmProviderConfig(
        name="Local",
        adapter_type="openai-compatible",
        credential_id=uuid4(),
        config={"base_url": "https://provider.test/v1"},
    )
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = SimpleNamespace(user_id=None)
    monkeypatch.setattr(service_module, "read_credential", lambda _credential: {"api_key": "stored"})
    remote_discovery = AsyncMock(return_value=[])
    monkeypatch.setattr(service_module.openai_compatible, "discover_models", remote_discovery)

    await ProviderService.discover_models(db, provider=provider)

    remote_discovery.assert_awaited_once_with(
        base_url="https://provider.test/v1",
        api_key="stored",
        timeout=30,
    )


async def test_provider_discovery_uses_codex_credential(monkeypatch) -> None:
    credential_id = uuid4()
    credential = type(
        "CredentialStub",
        (),
        {
            "id": credential_id,
            "kind": "codex-oauth",
            "label": "Codex",
            "metadata_json": {},
            "user_id": None,
        },
    )()
    provider = LlmProviderConfig(
        name="Codex",
        adapter_type="openai-codex",
        credential_id=credential_id,
    )
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = credential
    payload = {"access": "access", "refresh": "refresh", "account_id": "account"}
    monkeypatch.setattr(
        service_module,
        "refresh_codex_credential",
        AsyncMock(return_value=payload),
    )
    remote_discovery = AsyncMock(return_value=[])
    monkeypatch.setattr(service_module.codex_oauth, "discover_models", remote_discovery)

    await ProviderService.discover_models(db, provider=provider)

    remote_discovery.assert_awaited_once_with(
        access_token="access",
        account_id="account",
        timeout=30,
    )


async def test_provider_discovery_requires_codex_connection() -> None:
    provider = LlmProviderConfig(name="Codex", adapter_type="openai-codex")

    with pytest.raises(ProviderError) as exc_info:
        await ProviderService.discover_models(AsyncMock(spec=AsyncSession), provider=provider)

    assert exc_info.value.public_code == "provider.notConnected"


async def test_lemonade_discovery_maps_capabilities_and_filters_missing(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://lemonade.test/v1/models"
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "chat-vision",
                        "labels": ["Vision", "tool_calling"],
                        "recipe": " LlamaCpp ",
                        "downloaded": True,
                        "max_context_window": 8192,
                        "recipe_options": {"ctx_size": 4096},
                        "image_defaults": None,
                    },
                    {
                        "id": "image",
                        "labels": ["IMAGE"],
                        "recipe": "sd-cpp",
                        "downloaded": True,
                        "image_defaults": {"steps": 4, "cfg_scale": 1.0},
                    },
                    {
                        "id": "chat-parallel",
                        "labels": [],
                        "recipe": "llamacpp",
                        "downloaded": True,
                        "context_length": 6000,
                        "max_context_window": 12000,
                        "recipe_options": {"ctx_size": 4000},
                        "supports_parallel_tool_calls": True,
                    },
                    {
                        "id": "music",
                        "labels": [],
                        "recipe": "ACE_STEP",
                        "downloaded": True,
                    },
                    {
                        "id": "omni",
                        "labels": ["image", "tts"],
                        "recipe": "collection.omni",
                        "downloaded": True,
                    },
                    {
                        "id": "missing",
                        "labels": ["transcription"],
                        "recipe": "whispercpp",
                        "downloaded": False,
                    },
                ]
            },
        )

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        lemonade.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    models = await lemonade.discover_models(
        base_url="https://lemonade.test/v1", api_key="secret"
    )

    assert [item.id for item in models] == [
        "chat-vision",
        "image",
        "chat-parallel",
        "music",
        "omni",
    ]
    assert models[0].capabilities == {ModelCapability.CHAT}
    assert models[0].metadata["supports_image_input"] is True
    assert models[0].metadata["supports_tools"] is True
    assert models[0].metadata["supports_parallel_tool_calls"] is False
    assert models[0].metadata["context_length"] == 4096
    assert models[0].metadata["max_context_window"] == 8192
    assert models[1].capabilities == {ModelCapability.IMAGE_GENERATION}
    assert models[2].metadata["supports_tools"] is True
    assert models[2].metadata["supports_parallel_tool_calls"] is True
    assert models[2].metadata["context_length"] == 6000
    assert models[3].capabilities == {ModelCapability.AUDIO_GENERATION}
    assert ModelCapability.CHAT not in models[4].capabilities
    assert "capabilities" not in models[1].metadata


async def test_lemonade_complete_explicitly_uses_chat_completions(monkeypatch) -> None:
    provider = LlmProviderConfig(
        name="Lemonade",
        adapter_type="lemonade",
        pseudonymization_enabled=False,
        config={"base_url": "http://lemonade.test/v1", "api_type": "responses"},
    )
    model = ModelConfig(
        provider_id=provider.id,
        model_id="chat",
        reasoning_effort="high",
    )
    transport = AsyncMock(return_value=ProviderResult(content="OK"))
    responses = AsyncMock(side_effect=AssertionError("Responses API must not be used"))
    monkeypatch.setattr(service_module, "chat_completions", transport)
    monkeypatch.setattr(service_module, "responses_api", responses)

    result = await ProviderService.complete(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        messages=[{"role": "user", "content": "Test"}],
        tools=[],
        reasoning_effort="none",
    )

    assert result.content == "OK"
    transport.assert_awaited_once()
    responses.assert_not_awaited()
    assert transport.await_args.kwargs["request"].include_usage is True
    assert transport.await_args.kwargs["request"].reasoning_effort == "none"
    assert transport.await_args.kwargs["request"].enable_thinking is False


async def test_openai_compatible_stream_requests_usage(monkeypatch) -> None:
    provider = LlmProviderConfig(
        name="OpenAI-compatible",
        adapter_type="openai-compatible",
        pseudonymization_enabled=False,
        config={"base_url": "http://provider.test/v1"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="chat")
    transport = AsyncMock(return_value=ProviderResult(content="OK"))
    monkeypatch.setattr(service_module, "chat_completions", transport)

    await ProviderService.complete(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        messages=[{"role": "user", "content": "Test"}],
        tools=[],
    )

    assert transport.await_args.kwargs["request"].include_usage is True


async def test_qwen_3_8_uses_medium_by_default_and_disables_thinking_cleanly(
    monkeypatch,
) -> None:
    provider = LlmProviderConfig(
        name="Local",
        adapter_type="openai-compatible",
        pseudonymization_enabled=False,
        config={"base_url": "http://provider.test/v1"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="Qwen/Qwen3.8-27B")
    transport = AsyncMock(return_value=ProviderResult(content="OK"))
    monkeypatch.setattr(service_module, "chat_completions", transport)

    await ProviderService.complete(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        messages=[{"role": "user", "content": "Test"}],
        tools=[],
    )
    request = transport.await_args.kwargs["request"]
    assert request.reasoning_effort == ReasoningEffort.MEDIUM
    assert request.enable_thinking is True

    await ProviderService.complete(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        messages=[{"role": "user", "content": "Test"}],
        tools=[],
        reasoning_effort="none",
    )
    request = transport.await_args.kwargs["request"]
    assert request.reasoning_effort is None
    assert request.enable_thinking is False

    with pytest.raises(ProviderError) as exc_info:
        await ProviderService.complete(
            AsyncMock(spec=AsyncSession),
            provider=provider,
            model=model,
            messages=[{"role": "user", "content": "Test"}],
            tools=[],
            reasoning_effort="high",
        )
    assert exc_info.value.public_code == "provider.modelCapabilityUnavailable"


@pytest.mark.parametrize("effort", [None, "none", "minimal", "low", "medium", "high", "xhigh"])
async def test_halogen_reasoning_options_reach_chat_transport(monkeypatch, effort):
    from mato_loop_backend.api.routes.providers import (
        _model_reasoning_default,
        _model_reasoning_options,
    )

    provider = LlmProviderConfig(
        name="Local", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "http://provider.test/v1", "api_type": "chat_completions"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="halogen-qwen3.8-flash-next")
    assert _model_reasoning_default(provider, model) == "xhigh"
    assert _model_reasoning_options(provider, model) == [
        "none", "minimal", "low", "medium", "high", "xhigh",
    ]
    transport = AsyncMock(return_value=ProviderResult(content="OK"))
    monkeypatch.setattr(service_module, "chat_completions", transport)
    await ProviderService.complete(
        AsyncMock(spec=AsyncSession), provider=provider, model=model,
        messages=[{"role": "user", "content": "Reply with OK."}], tools=[],
        reasoning_effort=effort,
    )
    body = _chat_body(transport.await_args.kwargs["request"])
    assert body["chat_template_kwargs"] == {"enable_thinking": effort != "none"}
    if effort == "none":
        assert "reasoning_effort" not in body
    else:
        assert body["reasoning_effort"] == (effort or "xhigh")
    assert "temperature" not in body


async def test_complete_rejects_unknown_adapter_before_transport(monkeypatch) -> None:
    provider = LlmProviderConfig(
        name="Unknown", adapter_type="unknown", config={"base_url": "http://unknown.test"}
    )
    model = ModelConfig(provider_id=provider.id, model_id="chat")
    transport = AsyncMock()
    monkeypatch.setattr(service_module, "chat_completions", transport)

    with pytest.raises(ProviderError) as exc_info:
        await ProviderService.complete(
            AsyncMock(spec=AsyncSession),
            provider=provider,
            model=model,
            messages=[],
            tools=[],
        )

    assert exc_info.value.public_code == "provider.configurationInvalid"
    transport.assert_not_awaited()


@pytest.mark.parametrize(
    ("manual", "provider_cap", "helper", "expected"),
    [(None, None, None, None), (80_000, None, None, 80_000),
     (80_000, 40_000, None, 40_000), (None, 40_000, 7, 7),
     (2, 40_000, 7, 2), (None, None, 1, 1)],
)
def test_output_budget_uses_only_present_caps(manual, provider_cap, helper, expected):
    model = ModelConfig(
        provider_id=uuid4(), model_id="arbitrary",
        max_output_tokens=manual, discovered_max_output_tokens=provider_cap,
    )
    assert output_token_limit(model, helper) == expected
    assert generation_reserve(model) == min(2048, manual or 2048, provider_cap or 2048)
    assert allocate_output_tokens(model, 1000, requested=helper) == min(
        128_000 - 4096 - 1100, expected or 128_000
    )
    assert model.max_output_tokens == manual


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "100"])
@pytest.mark.parametrize("source", ["max_output_tokens", "discovered_max_output_tokens", "requested"])
def test_output_budget_rejects_invalid_caps(value, source):
    model = ModelConfig(provider_id=uuid4(), model_id="arbitrary")
    requested = None
    if source == "requested":
        requested = value
    else:
        model = model.model_copy(update={source: value})
    with pytest.raises(ProviderError) as exc_info:
        output_token_limit(model, requested)
    assert exc_info.value.public_code == "provider.configurationInvalid"


def test_output_budget_reserve_and_nonpositive_boundaries():
    model = ModelConfig(provider_id=uuid4(), model_id="arbitrary", context_window_tokens=8192)
    assert guarded_input_tokens(1) == 2
    assert allocate_output_tokens(model, 0, reserved_output_tokens=4096) == 4096
    for kwargs in (
        {"estimated_input_tokens": 1, "reserved_output_tokens": 4096},
        {"estimated_input_tokens": 4000},
        {"estimated_input_tokens": 0, "requested": 1, "reserved_output_tokens": 2},
    ):
        with pytest.raises(ProviderError) as exc_info:
            allocate_output_tokens(model, **kwargs)
        assert exc_info.value.code == "agent.contextWindowExceeded"
    assert allocate_output_tokens(model, 3722, requested=1) == 1


def test_raw_input_budget_is_exact_inverse_of_guarded_input():
    for capacity in (*range(1000), 59_392, 10_000_000):
        raw = raw_input_budget(capacity)
        assert guarded_input_tokens(raw) <= capacity
        assert guarded_input_tokens(raw + 1) > capacity
    assert raw_input_budget(59_392) == 53_992
    for invalid in (-1, True, 1.5, "100"):
        with pytest.raises(ProviderError) as exc_info:
            raw_input_budget(invalid)
        assert exc_info.value.public_code == "provider.configurationInvalid"


@pytest.mark.parametrize("api_type", ["chat_completions", "responses"])
async def test_output_budget_omits_unknown_cap_and_remains_name_invariant(monkeypatch, api_type):
    provider = LlmProviderConfig(
        name="Local", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "https://local.test/v1", "api_type": api_type},
    )
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [], "status": "completed", "output": []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "mato_loop_backend.providers.openai_compatible.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    budgets = []
    for name, content, projection in (
        ("arbitrary", "Test", None), ("Qwen/Qwen3.8-27B", "Test", None),
        ("gpt-5.6", "Test", None), ("arbitrary", "x" * 40_000, None),
        ("arbitrary", "Test", 20_000),
    ):
        model = ModelConfig(provider_id=provider.id, model_id=name, supports_streaming=False)
        result = await ProviderService.complete(
            AsyncMock(spec=AsyncSession), provider=provider, model=model,
            messages=[{"role": "system", "content": "Instructions"},
                      {"role": "user", "content": content}],
            tools=[{"name": "read", "parameters": {"type": "object"}}],
            estimated_input_tokens=projection,
        )
        budget = result.provider_data["output_budget"]
        budgets.append(budget)
        field = "max_tokens" if api_type == "chat_completions" else "max_output_tokens"
        assert field not in bodies[-1]
        assert budget["transmittedMaxOutputTokens"] is None
        assert budget["allocatedOutputTokens"] == (
            model.context_window_tokens - 4096 - budget["guardedInputTokens"]
        )
        assert budget["finalEstimatedInputTokens"] == estimate_request_input_tokens(bodies[-1])
    assert budgets[0] == budgets[1] == budgets[2]
    assert budgets[0]["allocatedOutputTokens"] > 16_384
    assert budgets[3]["allocatedOutputTokens"] < budgets[0]["allocatedOutputTokens"]
    assert budgets[4]["guardedInputTokens"] == 22_000


@pytest.mark.parametrize("api_type", ["chat_completions", "responses"])
@pytest.mark.parametrize(
    ("manual", "provider_cap", "helper", "expected"),
    [(80_000, None, None, 80_000), (80_000, 40_000, None, 40_000),
     (None, 40_000, 7, 7), (3, 40_000, 7, 3)],
)
async def test_output_budget_transports_minimum_cap(
    monkeypatch, api_type, manual, provider_cap, helper, expected,
):
    provider = LlmProviderConfig(
        name="Local", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "https://local.test/v1", "api_type": api_type},
    )
    model = ModelConfig(
        provider_id=provider.id, model_id="arbitrary", max_output_tokens=manual,
        discovered_max_output_tokens=provider_cap,
    )
    transport = AsyncMock(return_value=ProviderResult())
    monkeypatch.setattr(service_module, "chat_completions", transport)
    monkeypatch.setattr(service_module, "responses_api", transport)
    result = await ProviderService.complete(
        AsyncMock(spec=AsyncSession), provider=provider, model=model,
        messages=[{"role": "user", "content": "Test"}], tools=[], max_output_tokens=helper,
    )
    request = transport.await_args.kwargs["request"]
    body = _responses_body(request) if api_type == "responses" else _chat_body(request)
    field = "max_output_tokens" if api_type == "responses" else "max_tokens"
    budget = result.provider_data["output_budget"]
    assert body[field] == budget["allocatedOutputTokens"] == expected
    assert budget["manualLimit"] == model.max_output_tokens == manual
    assert budget["providerLimit"] == provider_cap
    assert budget["helperLimit"] == helper


@pytest.mark.parametrize(
    "kwargs",
    [{"estimated_input_tokens": -1}, {"estimated_input_tokens": True},
     {"estimated_input_tokens": 1.5}, {"safety_reserve_tokens": -1},
     {"reserved_output_tokens": -1}, {"reserved_output_tokens": True},
     {"max_output_tokens": 0}, {"max_output_tokens": False}],
)
async def test_output_budget_invalid_request_counts_fail_before_transport(monkeypatch, kwargs):
    provider = LlmProviderConfig(
        name="Local", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "https://local.test/v1"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="arbitrary")
    transport = AsyncMock()
    monkeypatch.setattr(service_module, "chat_completions", transport)
    with pytest.raises(ProviderError) as exc_info:
        await ProviderService.complete(
            AsyncMock(spec=AsyncSession), provider=provider, model=model,
            messages=[{"role": "user", "content": "Test"}], tools=[], **kwargs,
        )
    assert exc_info.value.public_code == "provider.configurationInvalid"
    transport.assert_not_awaited()


@pytest.mark.parametrize("api_type", ["chat_completions", "responses"])
async def test_output_budget_rechecks_pseudonym_expansion_and_required_reserve(monkeypatch, api_type):
    provider = LlmProviderConfig(
        name="Public", adapter_type="openai-compatible",
        config={"base_url": "https://public.test/v1", "api_type": api_type},
    )
    model = ModelConfig(provider_id=provider.id, model_id="arbitrary", context_window_tokens=8192)

    class ExpandingPseudonymizer(service_module.RequestPseudonymizer):
        def protect_messages(self, messages):
            return [{"role": "system", "content": "privacy " * 100},
                    {"role": "user", "content": "placeholder " * 400}]

        def restore_text(self, text, **kwargs):
            return text

    monkeypatch.setattr(service_module, "RequestPseudonymizer", ExpandingPseudonymizer)
    transport = AsyncMock(return_value=ProviderResult())
    monkeypatch.setattr(service_module, "chat_completions", transport)
    monkeypatch.setattr(service_module, "responses_api", transport)
    kwargs = dict(
        provider=provider, model=model, messages=[{"role": "user", "content": "a"}],
        tools=[], estimated_input_tokens=1,
    )
    result = await ProviderService.complete(AsyncMock(spec=AsyncSession), **kwargs)
    budget = result.provider_data["output_budget"]
    assert budget["estimatedInputTokens"] > 1000
    assert budget["allocatedOutputTokens"] < 3000
    transport.reset_mock()
    with pytest.raises(ProviderError) as exc_info:
        await ProviderService.complete(
            AsyncMock(spec=AsyncSession), **kwargs, reserved_output_tokens=3000,
        )
    assert exc_info.value.code == "agent.contextWindowExceeded"
    transport.assert_not_awaited()
    with pytest.raises(ProviderError):
        await ProviderService.complete(
            AsyncMock(spec=AsyncSession), **{**kwargs, "model": model.model_copy(
                update={"context_window_tokens": 4096}
            )},
        )
    transport.assert_not_awaited()


@pytest.mark.parametrize("serializer", [_chat_body, _responses_body])
def test_output_budget_estimates_framing_tools_and_replayed_continuation(serializer):
    request = ProviderRequest(model="ignored", messages=[{"role": "user", "content": "a"}])
    initial = estimate_request_input_tokens(serializer(request))
    request.max_output_tokens = 99_999
    request.temperature = 0.9
    request.model = "much-longer-model-name" * 100
    assert estimate_request_input_tokens(serializer(request)) == initial
    request.messages.insert(0, {"role": "system", "content": "instructions" * 100})
    request.tools = [{"name": "tool", "description": "description" * 100, "parameters": {}}]
    with_tools = estimate_request_input_tokens(serializer(request))
    assert with_tools > initial + 500
    request.continuation_scope = {"providerId": "same"}
    request.messages.append({
        "role": "assistant", "content": "",
        "provider_data": {
            "continuation_scope": request.continuation_scope,
            "chat_reasoning_details": [{"type": "reasoning.text", "text": "reason" * 1000}],
            "responses_output_items": [{"role": "assistant", "content": [
                {"type": "output_text", "text": "reason" * 1000}
            ]}],
        },
    })
    assert estimate_request_input_tokens(serializer(request)) > with_tools + 1000


@pytest.mark.parametrize("serializer", [_chat_body, _responses_body])
def test_output_budget_image_and_opaque_payloads_are_not_text(serializer):
    request = ProviderRequest(model="vision", messages=[{"role": "user", "content": "a"}])
    initial = estimate_request_input_tokens(serializer(request))
    request.media = [ProviderMedia(0, "image/png", data_url="data:image/png;base64,a", status="ready")]
    small = estimate_request_input_tokens(serializer(request))
    request.media[0].data_url += "a" * 1_000_000
    assert estimate_request_input_tokens(serializer(request)) == small
    assert initial + 2048 < small < initial + 2200
    request.messages.append({
        "role": "assistant", "content": "", "provider_data": {
            "continuation_scope": {"providerId": "same"},
            "reasoning_encrypted_content": "a" * 1_000_000,
            "chat_reasoning_details": [{"type": "reasoning.encrypted", "data": "a" * 1_000_000}],
        },
    })
    request.continuation_scope = {"providerId": "same"}
    assert small + 8192 < estimate_request_input_tokens(serializer(request)) < small + 8400


@pytest.mark.parametrize("modality", ["audio", "video"])
@pytest.mark.parametrize("helper", [None, 7, 16_384])
@pytest.mark.parametrize("manual", [None, 8_192])
async def test_output_budget_large_native_media_has_only_explicit_caps(
    monkeypatch, modality, helper, manual,
):
    provider = LlmProviderConfig(
        name="Local", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "https://local.test/v1"},
    )
    model = ModelConfig(
        provider_id=provider.id, model_id="media", input_modalities={"text", modality},
        max_output_tokens=manual,
    )
    transport = AsyncMock(return_value=ProviderResult())
    monkeypatch.setattr(service_module, "chat_completions", transport)
    result = await ProviderService.complete(
        AsyncMock(spec=AsyncSession), provider=provider, model=model,
        messages=[{"role": "user", "content": "Inspect"}], tools=[], max_output_tokens=helper,
        media_inputs=[ProviderMediaInput(
            0, b"a" * (17 * 1024 * 1024),
            "audio/wav" if modality == "audio" else "video/mp4", "private", modality,
        )],
    )
    budget = result.provider_data["output_budget"]
    assert 8192 < budget["estimatedInputTokens"] < 8400
    assert budget["mediaInputTokensUnknown"] is True
    capacity = model.context_window_tokens - 4096 - budget["guardedInputTokens"]
    assert budget["allocatedOutputTokens"] == min(
        value for value in (capacity, manual, helper) if value is not None
    )
    assert budget["helperLimit"] == helper
    if helper is None:
        assert budget["allocatedOutputTokens"] > 2048
        assert budget["mode"] == ("auto" if manual is None else "manual")
    assert transport.await_args.kwargs["request"].max_output_tokens == (
        budget["allocatedOutputTokens"]
        if manual is not None or helper is not None
        else None
    )


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("usage,known", [
    ({"reasoning_tokens": 0, "output_tokens": 100}, True),
    ({"reasoning_tokens": 32, "output_tokens": 100}, True),
    ({"reasoning_tokens": 20_000, "output_tokens": 21_000}, True),
    (None, False), ({}, False),
    ({"reasoning_tokens": -1, "output_tokens": 100}, False),
    ({"reasoning_tokens": True, "output_tokens": 100}, False),
    ({"reasoning_tokens": "32", "output_tokens": 100}, False),
    ({"reasoning_tokens": 32.0, "output_tokens": 100}, False),
    ({"reasoning_tokens": 32}, False),
    ({"reasoning_tokens": 101, "output_tokens": 100}, False),
])
def test_responses_opaque_usage_is_local_and_charged_per_source_response(legacy, usage, known):
    scope = {"providerId": "same"}
    data = {"continuation_scope": scope, "usage": usage,
            "reasoning_encrypted_content": "legacy"}
    if not legacy:
        data["responses_output_items"] = [
            {"type": "reasoning", "encrypted_content": f"opaque-{index}"}
            for index in range(2)
        ]
    request = ProviderRequest(
        model="test", continuation_scope=scope,
        messages=[{"role": "user", "content": "Inspect"}, {
            "role": "assistant", "content": "", "provider_data": data,
            "tool_calls": [{"id": "call", "name": "browser", "arguments": {"action": "tabs"}}],
        }],
        media=[ProviderMedia(0, "image/png", data_url="data:image/png;base64,a", status="ready")],
    )
    original = deepcopy(request)
    estimates = {}
    body = _responses_body(request, reasoning_token_estimates=estimates)
    assert body == _responses_body(request)
    assert request == original
    count = 1 if legacy else 2
    if known:
        reserve = (usage["reasoning_tokens"] + 256 + count - 1) // count
        assert list(estimates.values()) == [reserve] * count
        assert all(body["input"][index]["type"] == "reasoning" for index in estimates)
    else:
        assert estimates == {}
    assert estimate_request_input_tokens(body, reasoning_token_estimates=estimates) == (
        estimate_request_input_tokens(body) - (count * 8192 if known else 0)
        + sum(estimates.values())
    )
    request.continuation_scope = {"providerId": "different"}
    body = _responses_body(request, reasoning_token_estimates=estimates)
    assert estimates == {}
    assert not any(item.get("encrypted_content") for item in body["input"])


async def test_long_responses_tool_chain_uses_same_estimate_before_transport(monkeypatch):
    provider = LlmProviderConfig(
        name="Cloud", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "https://cloud.test/v1", "api_type": "responses"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="test", context_window_tokens=272_000)
    messages = [{"role": "user", "content": "Test the browser"}]
    first = await ProviderService.prepare_request(provider=provider, model=model, messages=messages, tools=[])
    for index in range(40):
        messages.extend([{
            "role": "assistant", "content": "",
            "tool_calls": [{"id": str(index), "name": "browser", "arguments": {"action": "tabs"}}],
            "provider_data": {
                "continuation_scope": first.request.continuation_scope,
                "usage": {"reasoning_tokens": 12, "output_tokens": 80},
                "responses_output_items": [{"type": "reasoning", "encrypted_content": f"opaque-{index}"}],
            },
        }, {"role": "tool", "tool_call_id": str(index), "content": "{\"tabs\":[]}"}])
    original = deepcopy(messages)
    prepared = await ProviderService.prepare_request(provider=provider, model=model, messages=messages, tools=[])
    body = _responses_body(prepared.request)
    assert estimate_request_input_tokens(body) > model.context_window_tokens
    assert prepared.estimated_input_tokens < 20_000
    session = ChatSession(id=uuid4())
    records = [Message(id=uuid4(), session_id=session.id, **deepcopy(message)) for message in messages]
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None
    summarize = AsyncMock()

    async def estimate(projected):
        return (await ProviderService.prepare_request(
            provider=provider, model=model, messages=projected, tools=[],
        )).estimated_input_tokens

    projection = await ContextManager.build(
        db, chat_session=session, records=records, system_prompt="Use the browser", tool_schemas=[],
        model=model, settings=AgentRuntimeSettings(), summarize=summarize, estimate_request=estimate,
    )
    summarize.assert_not_awaited()
    db.commit.assert_not_awaited()
    projected = await ProviderService.prepare_request(
        provider=provider, model=model, messages=projection.messages, tools=[],
    )
    assert projection.estimated_input_tokens == projected.estimated_input_tokens
    transport = AsyncMock(return_value=ProviderResult())
    monkeypatch.setattr(service_module, "responses_api", transport)
    result = await ProviderService.complete(
        AsyncMock(spec=AsyncSession), provider=provider, model=model, messages=messages, tools=[],
        estimated_input_tokens=prepared.estimated_input_tokens,
    )
    budget = result.provider_data["output_budget"]
    assert budget["finalEstimatedInputTokens"] == prepared.estimated_input_tokens
    sent = _responses_body(transport.await_args.kwargs["request"])
    assert sent["input"] == body["input"]
    assert len([item for item in sent["input"] if item.get("encrypted_content")]) == 40
    assert messages == original
    # Reported reasoning above the old fixed reserve still consumes real capacity.
    messages[-2]["provider_data"]["usage"] = {"reasoning_tokens": 300_000, "output_tokens": 300_100}
    transport.reset_mock()
    with pytest.raises(ProviderError) as error:
        await ProviderService.complete(
            AsyncMock(spec=AsyncSession), provider=provider, model=model, messages=messages, tools=[],
        )
    assert error.value.code == "agent.contextWindowExceeded"
    transport.assert_not_awaited()


@pytest.mark.parametrize("api_type", ["chat_completions", "responses"])
@pytest.mark.parametrize(("manual", "helper"), [(None, 128), (128, None)])
async def test_complete_loads_configured_safety_for_small_context_helpers(
    monkeypatch, api_type, manual, helper,
):
    provider = LlmProviderConfig(
        name="Local", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "https://local.test/v1", "api_type": api_type},
    )
    model = ModelConfig(
        provider_id=provider.id, model_id="small", context_window_tokens=512,
        max_output_tokens=manual,
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = SimpleNamespace(data={"safety_reserve_tokens": 0})
    transport = AsyncMock(return_value=ProviderResult(content="Short title"))
    monkeypatch.setattr(service_module, "chat_completions", transport)
    monkeypatch.setattr(service_module, "responses_api", transport)
    kwargs = dict(
        provider=provider, model=model, messages=[{"role": "user", "content": "Name this chat"}],
        tools=[], max_output_tokens=helper,
    )
    result = await ProviderService.complete(db, **kwargs)
    db.scalar.assert_awaited_once()
    statement = db.scalar.await_args.args[0]
    assert "agent-runtime" in statement.compile().params.values()
    assert "settings.user_id IS NULL" in str(statement)
    assert result.provider_data["output_budget"]["safetyReserveTokens"] == 0
    assert transport.await_args.kwargs["request"].max_output_tokens == 128

    db.scalar.return_value = SimpleNamespace(data={"safety_reserve_tokens": 512})
    transport.reset_mock()
    with pytest.raises(ProviderError) as exc_info:
        await ProviderService.complete(db, **kwargs)
    assert exc_info.value.code == "agent.contextWindowExceeded"
    assert db.scalar.await_count == 2
    transport.assert_not_awaited()


@pytest.mark.parametrize("safety", [0, 4096])
async def test_complete_explicit_safety_snapshot_skips_settings_lookup(monkeypatch, safety):
    provider = LlmProviderConfig(
        name="Local", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "https://local.test/v1"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="model")
    load = AsyncMock(side_effect=AssertionError("Explicit safety must not load settings"))
    monkeypatch.setattr(service_module.SettingsStore, "load", load)
    monkeypatch.setattr(service_module, "chat_completions", AsyncMock(return_value=ProviderResult()))
    result = await ProviderService.complete(
        AsyncMock(spec=AsyncSession), provider=provider, model=model,
        messages=[{"role": "user", "content": "Test"}], tools=[], safety_reserve_tokens=safety,
    )
    load.assert_not_awaited()
    budget = result.provider_data["output_budget"]
    assert budget["safetyReserveTokens"] == safety
    assert budget["allocatedOutputTokens"] == (
        model.context_window_tokens - safety - budget["guardedInputTokens"]
    )


@pytest.mark.parametrize(
    ("adapter_type", "api_type"),
    [("openai-compatible", "chat_completions"), ("openai-compatible", "responses"),
     ("openai-codex", "responses"), ("lemonade", "chat_completions")],
)
async def test_prepare_is_local_and_matches_transmitted_request(monkeypatch, adapter_type, api_type):
    provider = LlmProviderConfig(
        name="Test", adapter_type=adapter_type, credential_id=uuid4(),
        pseudonymization_enabled=False,
        config={"base_url": "https://local.test/v1", "api_type": api_type},
    )
    model = ModelConfig(
        provider_id=provider.id, model_id="openai-codex/test" if adapter_type == "openai-codex" else "test",
        supports_streaming=False, supports_image_input=True,
        discovery_metadata={"recipe": "llamacpp", "owned_by": "llamacpp"},
    )
    tools = [{"name": "inspect", "parameters": {
        "type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 200},
    }}]
    messages = [{"role": "system", "content": "Instructions"},
                {"role": "user", "content": "Inspect"},
                {"role": "assistant", "content": "Checked", "tool_calls": [
                    {"id": "call", "name": "inspect", "arguments": {"text": '\\"\n' * 100}},
                ]},
                {"role": "tool", "tool_call_id": "call", "content": "Result"}]
    original = deepcopy((messages, tools))
    load = AsyncMock(return_value=SimpleNamespace())
    decrypt = Mock(return_value={"api_key": "dummy"})
    refresh = AsyncMock(return_value={"access": "dummy", "account_id": "test"})
    settings = AsyncMock(side_effect=AssertionError("Preparation must not load settings"))
    adapter = service_module.PROVIDER_ADAPTERS[adapter_type]
    operation = AsyncMock(wraps=adapter.operation)
    monkeypatch.setattr(service_module, "PROVIDER_ADAPTERS", {
        **service_module.PROVIDER_ADAPTERS, adapter_type: replace(adapter, operation=operation),
    })
    monkeypatch.setattr(service_module, "load_credential", load)
    monkeypatch.setattr(service_module, "read_credential", decrypt)
    monkeypatch.setattr(service_module, "refresh_codex_credential", refresh)
    monkeypatch.setattr(service_module.SettingsStore, "load", settings)
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [], "status": "completed", "output": []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "mato_loop_backend.providers.openai_compatible.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    options = dict(
        provider=provider, model=model, messages=messages, tools=tools,
        tool_choice="required", prompt_cache_key="test-session",
        target_trust_level=TrustLevel.PRIVATE, reasoning_effort=ReasoningEffort.NONE,
        image_inputs=[ProviderImageInput(
            message_index=1, data=_png_bytes(), mime_type="image/png", trust_level="private",
        )],
    )
    prepared = await ProviderService.prepare_request(**options)
    assert prepared.api_type == api_type
    assert prepared.request.continuation_scope["trustLevel"] == "private"
    assert "Instructions" not in repr(prepared)
    assert prepared.request.max_output_tokens is None
    load.assert_not_awaited()
    decrypt.assert_not_called()
    refresh.assert_not_awaited()
    settings.assert_not_awaited()
    operation.assert_not_awaited()
    assert bodies == []

    with pytest.raises(ProviderError, match="Insufficient context"):
        await ProviderService.complete(
            AsyncMock(spec=AsyncSession), **options,
            safety_reserve_tokens=model.context_window_tokens,
        )
    load.assert_not_awaited()
    decrypt.assert_not_called()
    refresh.assert_not_awaited()
    operation.assert_not_awaited()
    assert bodies == []

    result = await ProviderService.complete(
        AsyncMock(spec=AsyncSession), **options, safety_reserve_tokens=0,
        estimated_input_tokens=prepared.estimated_input_tokens,
    )
    assert len(bodies) == 1
    assert prepared.estimated_input_tokens == estimate_request_input_tokens(
        bodies[0], model.characters_per_token,
    ) == result.provider_data["output_budget"]["finalEstimatedInputTokens"]
    load.assert_awaited_once()
    operation.assert_awaited_once()
    assert refresh.await_count == int(adapter.codex_auth)
    assert decrypt.call_count == int(not adapter.codex_auth)
    settings.assert_not_awaited()
    assert (messages, tools) == original
