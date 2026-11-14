import json

import httpx
import pytest
from aiohttp import web

from mato_loop_backend.providers.base import ProviderError
from mato_loop_backend.providers.realtime import transport

CONFIG = transport.RealtimeSessionConfig(
    model="voice-deployment",
    voice="marin",
    transcription_model="transcription-deployment",
    instructions="Speak briefly. Delegate work to MatoLoop.",
)


def mock_client(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        transport.httpx, "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.parametrize(
    ("adapter", "base", "auth_header", "auth_value", "location"),
    [
        ("azure-openai-realtime", "https://voice.openai.azure.com/openai/v1", "api-key", "test-key", None),
        ("azure-openai-realtime", "https://voice.openai.azure.com/openai/v1", "api-key", "test-key", "/v1/realtime/calls/rtc_test"),
        ("openai-realtime", "https://api.openai.com/v1", "authorization", "Bearer test-key", None),
        ("openai-realtime", "https://voice.example.com/custom/v1", "authorization", "Bearer test-key", None),
    ],
)
async def test_ga_negotiation_keeps_credentials_server_side_and_audio_disabled(
    monkeypatch, adapter, base, auth_header, auth_value, location,
):
    requests = []

    def handler(request):
        requests.append(request)
        assert not request.url.query
        assert "OpenAI-Beta" not in request.headers
        if request.url.path.endswith("/client_secrets"):
            assert request.headers[auth_header] == auth_value
            config = json.loads(request.content)["session"]
            assert config["model"] == "voice-deployment"
            assert config["type"] == "realtime"
            assert config["output_modalities"] == ["text"]
            assert config["audio"]["input"]["turn_detection"] is None
            assert config["audio"]["input"]["transcription"]["model"] == "transcription-deployment"
            assert config["tools"] == []
            return httpx.Response(200, json={"value": "ephemeral"})
        assert request.url.path.endswith("/realtime/calls")
        assert request.headers["authorization"] == "Bearer ephemeral"
        assert "api-key" not in request.headers
        assert request.headers["content-type"] == "application/sdp"
        assert request.content == b"v=0\r\n"
        return httpx.Response(
            201, text="v=0\r\nanswer",
            headers={"Location": location or f"{base}/realtime/calls/rtc_test"},
        )

    mock_client(monkeypatch, handler)
    endpoint = transport.RealtimeEndpoint(base, adapter)
    result = await transport.negotiate(
        endpoint, api_key="test-key", session=CONFIG, sdp_offer="v=0\r\n",
    )
    assert result.call_id == "rtc_test"
    assert result.sdp_answer == "v=0\r\nanswer"
    assert endpoint.controller_url(result.call_id) == f"{base.replace('https:', 'wss:')}/realtime?call_id=rtc_test"
    assert len(requests) == 2
    assert "ephemeral" not in repr(result)


@pytest.mark.parametrize(
    "location",
    [
        "https://other.example/v1/realtime/calls/rtc_test",
        "//other.example/v1/realtime/calls/rtc_test",
        "/v1/realtime/calls/rtc_test?key=secret",
        "/v1/realtime/calls/rtc_test#fragment",
        "/v1/realtime/calls/rtc_test/other",
        "/v1/realtime/calls/rtc_%2fother",
        "/v1/realtime/calls/invalid",
        "/v1/realtime/calls/rtc_",
        "",
    ],
)
def test_call_location_cannot_redirect_controller_credentials(location):
    endpoint = transport.RealtimeEndpoint("https://api.openai.com/v1", "openai-realtime")
    with pytest.raises(ProviderError, match="Invalid Realtime call location"):
        endpoint.call_id(location)


@pytest.mark.parametrize(
    ("base", "adapter"),
    [
        ("http://example.test/v1", "openai-realtime"),
        ("https://user:pass@example.test/v1", "openai-realtime"),
        ("https://example.test/v1?api-version=preview", "openai-realtime"),
        ("https://example.test/v1#fragment", "openai-realtime"),
        ("https://example.test/openai/realtime", "azure-openai-realtime"),
        ("https://example.test/v1", "azure-openai-realtime"),
        ("https://example.test/proxy/../v1", "openai-realtime"),
        ("https://example.test/%76%31", "openai-realtime"),
    ],
)
def test_only_explicit_ga_https_endpoints_are_accepted(base, adapter):
    with pytest.raises(ProviderError):
        transport.RealtimeEndpoint(base, adapter)


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "provider.authenticationFailed"), (429, "provider.rateLimited"), (302, "provider.requestFailed")],
)
async def test_upstream_errors_are_controlled_and_never_follow_redirects(monkeypatch, status, code):
    mock_client(monkeypatch, lambda request: httpx.Response(
        status, text="sensitive upstream response",
        headers={"Location": "https://other.example/v1"},
    ))
    with pytest.raises(ProviderError) as error:
        await transport.negotiate(
            transport.RealtimeEndpoint("https://api.openai.com/v1", "openai-realtime"),
            api_key="test-key", session=CONFIG, sdp_offer="v=0\r\n",
        )
    assert error.value.public_code == code
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize("payload", [{}, {"value": ""}, {"value": "token\r\ninjection"}, []])
async def test_invalid_ephemeral_token_cannot_start_call(monkeypatch, payload):
    mock_client(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ProviderError) as error:
        await transport.negotiate(
            transport.RealtimeEndpoint("https://api.openai.com/v1", "openai-realtime"),
            api_key="test-key", session=CONFIG, sdp_offer="v=0\r\n",
        )
    assert error.value.public_code == "provider.invalidResponse"


@pytest.mark.parametrize("redirect", [False, True])
async def test_controller_authenticates_without_forwarding_key_to_redirect(monkeypatch, redirect):
    received = []

    async def handler(request):
        received.append(request.path)
        assert request.headers["api-key"] == "test-key"
        if redirect and request.path == "/realtime":
            raise web.HTTPFound("/redirected")
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json({"type": "session.created"})
        async for _ in socket:
            pass
        return socket

    app = web.Application()
    app.router.add_get("/realtime", handler)
    app.router.add_get("/redirected", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    monkeypatch.setattr(
        transport.RealtimeEndpoint, "controller_url",
        lambda self, call_id: f"ws://127.0.0.1:{port}/realtime",
    )
    endpoint = transport.RealtimeEndpoint("https://voice.openai.azure.com/openai/v1", "azure-openai-realtime")
    try:
        if redirect:
            with pytest.raises(ProviderError, match="redirect rejected"):
                async with transport.connect_controller(endpoint, api_key="test-key", call_id="rtc_test"):
                    pytest.fail("A redirected controller must not connect")
        else:
            async with transport.connect_controller(endpoint, api_key="test-key", call_id="rtc_test") as socket:
                assert await socket.receive_json() == {"type": "session.created"}
    finally:
        await runner.cleanup()
    assert received == ["/realtime"]
