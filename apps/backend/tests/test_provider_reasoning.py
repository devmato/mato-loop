import json

import httpx
import pytest

from mato_loop_backend.providers.base import ProviderRequest
from mato_loop_backend.providers.openai_compatible import (
    _chat_body,
    _chat_messages,
    _chat_result,
    _direct_thinking_options,
    _responses_body,
    _responses_input,
    _responses_result,
    chat_completions,
    responses_api,
)
from mato_loop_backend.providers.reasoning import (
    TaggedReasoningStream,
    reasoning_profile,
    split_tagged_reasoning,
)
from mato_loop_backend.settings.models import ReasoningEffort


class _OpenAfterTerminalEvent(httpx.AsyncByteStream):
    def __init__(self, body: str) -> None:
        self.body = body.encode()

    async def __aiter__(self):
        yield self.body
        raise AssertionError("provider stream was read after its terminal event")


def test_tagged_reasoning_is_separated_from_answer() -> None:
    assert split_tagged_reasoning("<think>Prüfen und vergleichen.</think>Fertige Antwort", final=True) == (
        "Fertige Antwort",
        "Prüfen und vergleichen.",
    )
    assert split_tagged_reasoning("Normale Antwort", final=True) == ("Normale Antwort", "")
    assert split_tagged_reasoning(
        "<|channel>thought\nInterne Ausgabe<channel|><|channel>response\nAntwort",
        final=True,
    ) == ("Antwort", "Interne Ausgabe")


def test_tagged_reasoning_stream_handles_marker_chunk_boundaries() -> None:
    stream = TaggedReasoningStream()

    assert stream.add("<thi") == ("", "")
    assert stream.add("nk>Plan") == ("", "Plan")
    assert stream.add(" ausarbeiten</think>Antwort") == ("Antwort", " ausarbeiten")
    assert stream.finish() == ("Antwort", "Plan ausarbeiten")


def test_channel_response_marker_can_span_chunks() -> None:
    stream = TaggedReasoningStream()

    assert stream.add("<|channel>thought\nPlan<channel|><|chan") == ("", "Plan")
    assert stream.add("nel>response\nAntwort") == ("Antwort", "")
    assert stream.finish() == ("Antwort", "Plan")


def test_chat_result_exposes_explicit_reasoning_and_usage() -> None:
    result = _chat_result(
        {
            "choices": [
                {
                    "message": {
                        "reasoning_content": "Optionen verglichen.",
                        "content": "Ergebnis",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 8,
                "prompt_tokens_details": {
                    "cached_tokens": 7,
                    "cache_write_tokens": 3,
                },
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        }
    )

    assert result.content == "Ergebnis"
    assert result.reasoning is not None
    assert result.reasoning.text == "Optionen verglichen."
    assert result.provider_data["chat_reasoning_content"] == "Optionen verglichen."
    assert result.reasoning.reasoning_tokens == 5
    assert result.usage["cached_input_tokens"] == 7
    assert result.usage["cache_write_input_tokens"] == 3


def test_chat_result_preserves_private_reasoning_details_separately() -> None:
    details = [{"type": "reasoning.encrypted", "data": "opaque"}]
    result = _chat_result(
        {
            "choices": [
                {
                    "message": {
                        "reasoning": "Öffentlich begründet.",
                        "reasoning_details": details,
                        "content": "Antwort",
                    }
                }
            ]
        }
    )

    assert result.reasoning is not None
    assert result.reasoning.text == "Öffentlich begründet."
    assert result.provider_data["chat_reasoning_details"] == details
    assert "opaque" not in result.reasoning.text


@pytest.mark.parametrize("field", ["adapterType", "providerId", "modelConfigId", "credentialId", "endpoint", "trustLevel"])
def test_chat_reasoning_details_replay_only_in_matching_scope(field) -> None:
    scope = {
        "adapterType": "openai-compatible",
        "providerId": "provider-1",
        "modelConfigId": "model-1",
        "credentialId": None,
        "endpoint": "https://provider.test/v1",
        "trustLevel": "private",
    }
    message = {
        "role": "assistant",
        "content": "Antwort",
        "provider_data": {
            "chat_reasoning_details": [{"data": "opaque"}],
            "chat_reasoning_content": "Explicit reasoning",
            "continuation_scope": scope,
        },
    }

    assert "reasoning_details" in _chat_messages([message], scope)[0]
    assert _chat_messages([message], scope)[0]["reasoning_content"] == "Explicit reasoning"
    assert "reasoning_details" not in _chat_messages(
        [message], {**scope, field: "different"}
    )[0]
    assert "reasoning_content" not in _chat_messages([message], {**scope, field: "different"})[0]


def test_responses_result_exposes_summary_but_not_encrypted_state() -> None:
    result = _responses_result(
        {
            "status": "completed",
            "output": [
                {
                    "type": "reasoning",
                    "encrypted_content": "opaque-secret-state",
                    "summary": [{"type": "summary_text", "text": "Kurz begründet."}],
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Antwort"}],
                },
            ],
        }
    )

    assert result.reasoning is not None
    assert result.reasoning.text == "Kurz begründet."
    assert "opaque-secret-state" not in result.reasoning.text
    assert result.provider_data["reasoning_encrypted_content"] == "opaque-secret-state"
    assert result.provider_data["responses_output_items"][0]["summary"] == [
        {"type": "summary_text", "text": "Kurz begründet."}
    ]


def test_tagged_responses_reasoning_keeps_content_kind() -> None:
    result = _responses_result(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "<think>Plan</think>Antwort"}
                    ],
                }
            ],
        }
    )

    assert result.content == "Antwort"
    assert result.reasoning is not None
    assert result.reasoning.kind == "content"


def test_responses_summary_request_and_scoped_replay() -> None:
    scope = {
        "adapterType": "openai-compatible",
        "providerId": "provider-1",
        "modelConfigId": "model-1",
        "credentialId": None,
        "endpoint": "https://provider.test/v1",
    }
    request = ProviderRequest(
        model="reasoning-model",
        messages=[],
        reasoning_summary=True,
        continuation_scope=scope,
    )
    assert _responses_body(request)["reasoning"] == {"summary": "auto"}
    assert _responses_body(request)["include"] == ["reasoning.encrypted_content"]

    message = {
        "role": "assistant",
        "content": "Antwort",
        "provider_data": {
            "reasoning_encrypted_content": "encrypted",
            "continuation_scope": scope,
        },
    }
    _, matching = _responses_input([message], scope)
    _, foreign = _responses_input([message], {**scope, "providerId": "provider-2"})

    assert matching[0]["type"] == "reasoning"
    assert all(item.get("type") != "reasoning" for item in foreign)


def test_reasoning_effort_uses_native_openai_and_llama_cpp_fields() -> None:
    request = ProviderRequest(
        model="reasoning-model",
        messages=[],
        reasoning_summary=True,
        reasoning_effort=ReasoningEffort.NONE,
        enable_thinking=False,
    )

    assert _chat_body(request)["reasoning_effort"] == "none"
    assert _chat_body(request)["chat_template_kwargs"] == {"enable_thinking": False}
    assert _responses_body(request)["reasoning"] == {
        "summary": "auto",
        "effort": "none",
    }


def test_qwen_3_8_profile_and_thinking_parameter_locations() -> None:
    profile = reasoning_profile("Qwen/Qwen3.8-27B-Q4_K_M.gguf")
    assert profile is not None
    assert profile.default_effort == ReasoningEffort.MEDIUM
    assert profile.options == (
        ReasoningEffort.NONE,
        ReasoningEffort.LOW,
        ReasoningEffort.MEDIUM,
        ReasoningEffort.XHIGH,
    )
    assert reasoning_profile("Qwen3.5-27B") is None

    request = ProviderRequest(model="qwen3.8-27b", messages=[], enable_thinking=False)
    assert _chat_body(request)["chat_template_kwargs"] == {"enable_thinking": False}
    assert _chat_body(request, direct_thinking_options=True)["enable_thinking"] is False
    assert _direct_thinking_options(
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    )
    assert not _direct_thinking_options("http://localhost:8000/v1")


def test_codex_reasoning_profile_uses_catalog_metadata() -> None:
    profile = reasoning_profile(
        "openai-codex/gpt-5.6-sol",
        {
            "default_reasoning_effort": "low",
            "reasoning_efforts": ["low", "medium", "max", "ultra", "future"],
        },
    )

    assert profile is not None
    assert profile.default_effort == ReasoningEffort.LOW
    assert profile.options == (
        ReasoningEffort.LOW,
        ReasoningEffort.MEDIUM,
        ReasoningEffort.MAX,
        ReasoningEffort.ULTRA,
    )
    assert profile.supports_disable is False


def test_halogen_profile_is_exact_and_discovery_takes_precedence() -> None:
    assert reasoning_profile("halogen-unrelated") is None
    profile = reasoning_profile("halogen-qwen3.8-flash-next", {
        "reasoning_efforts": ["low", "medium"], "default_reasoning_effort": "low",
    })
    assert profile.default_effort == ReasoningEffort.LOW
    assert profile.options == (ReasoningEffort.LOW, ReasoningEffort.MEDIUM)


@pytest.mark.parametrize("explicit", [False, True])
async def test_chat_stream_routes_tagged_reasoning_separately(monkeypatch, explicit) -> None:
    events = [
        {"choices": [{"delta": {"content": "<thi"}}]},
        {"choices": [{"delta": {"content": "nk>Plan"}}]},
        {"choices": [{"delta": {"content": "en</think>Antwort"}, "finish_reason": "stop"}]},
    ]
    if explicit:
        events = [
            {"choices": [{"delta": {"reasoning_content": "Plan"}}]},
            {"choices": [{"delta": {"reasoning_content": "en"}}]},
            {"choices": [{"delta": {"content": "Antwort"}, "finish_reason": "stop"}]},
        ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, text=body))
    monkeypatch.setattr(
        "mato_loop_backend.providers.openai_compatible.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    answers = []
    reasoning = []

    async def on_delta(text: str) -> None:
        answers.append(text)

    async def on_reasoning(text: str, kind: str) -> None:
        reasoning.append((text, kind))

    result = await chat_completions(
        base_url="https://provider.test/v1",
        api_key=None,
        request=ProviderRequest(model="local", messages=[]),
        on_delta=on_delta,
        on_reasoning=on_reasoning,
    )

    assert answers == ["Antwort"]
    assert reasoning == [("Plan", "content"), ("en", "content")]
    assert result.content == "Antwort"
    assert result.reasoning is not None
    assert result.reasoning.text == "Planen"
    assert result.provider_data.get("chat_reasoning_content") == ("Planen" if explicit else None)


async def test_chat_stream_stops_at_done_without_waiting_for_eof(monkeypatch) -> None:
    stream = _OpenAfterTerminalEvent(
        'data: {"choices":[{"delta":{"content":"Antwort"},"finish_reason":"stop"}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n'
        "data: [DONE]\n\n"
    )
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream))
    monkeypatch.setattr(
        "mato_loop_backend.providers.openai_compatible.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = await chat_completions(
        base_url="https://provider.test/v1",
        api_key=None,
        request=ProviderRequest(model="local", messages=[]),
        on_delta=None,
    )

    assert result.content == "Antwort"
    assert result.usage == {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}


async def test_responses_stream_routes_summary_separately(monkeypatch) -> None:
    events = [
        {"type": "response.reasoning_summary_text.delta", "delta": "Kurz "},
        {"type": "response.reasoning_summary_text.delta", "delta": "begründet."},
        {"type": "response.output_text.delta", "delta": "Antwort"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "Kurz begründet."}],
                    }
                ],
            },
        },
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, text=body))
    monkeypatch.setattr(
        "mato_loop_backend.providers.openai_compatible.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    reasoning = []

    async def on_reasoning(text: str, kind: str) -> None:
        reasoning.append((text, kind))

    result = await responses_api(
        url="https://provider.test/v1/responses",
        headers={},
        request=ProviderRequest(model="reasoning", messages=[]),
        on_delta=None,
        on_reasoning=on_reasoning,
    )

    assert reasoning == [("Kurz ", "summary"), ("begründet.", "summary")]
    assert result.content == "Antwort"
    assert result.reasoning is not None
    assert result.reasoning.text == "Kurz begründet."


async def test_responses_stream_stops_at_completed_without_waiting_for_eof(
    monkeypatch,
) -> None:
    event = {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Antwort"}],
                }
            ],
        },
    }
    stream = _OpenAfterTerminalEvent(
        'data: {"type":"response.output_text.delta","delta":"Antwort"}\n\n'
        f"data: {json.dumps(event)}\n\n"
    )
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream))
    monkeypatch.setattr(
        "mato_loop_backend.providers.openai_compatible.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = await responses_api(
        url="https://provider.test/v1/responses",
        headers={},
        request=ProviderRequest(model="reasoning", messages=[]),
        on_delta=None,
    )

    assert result.content == "Antwort"
    assert result.finish_reason == "stop"


async def test_responses_stream_separates_summary_parts(monkeypatch) -> None:
    events = [
        {"type": "response.reasoning_summary_part.added"},
        {"type": "response.reasoning_summary_text.delta", "delta": "**Planung**"},
        {"type": "response.reasoning_summary_part.added"},
        {"type": "response.reasoning_summary_text.delta", "delta": "**Prüfung**"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [
                            {"type": "summary_text", "text": "**Planung**"},
                            {"type": "summary_text", "text": "**Prüfung**"},
                        ],
                    }
                ],
            },
        },
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, text=body))
    monkeypatch.setattr(
        "mato_loop_backend.providers.openai_compatible.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    reasoning = []

    async def on_reasoning(text: str, kind: str) -> None:
        reasoning.append((text, kind))

    result = await responses_api(
        url="https://provider.test/v1/responses",
        headers={},
        request=ProviderRequest(model="reasoning", messages=[]),
        on_delta=None,
        on_reasoning=on_reasoning,
    )

    assert reasoning == [
        ("**Planung**", "summary"),
        ("\n\n", "summary"),
        ("**Prüfung**", "summary"),
    ]
    assert result.reasoning is not None
    assert result.reasoning.text == "**Planung**\n\n**Prüfung**"
