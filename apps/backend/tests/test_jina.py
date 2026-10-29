import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from tool_helpers import _context

from mato_loop_backend.integrations.jina import client, service
from mato_loop_backend.integrations.jina.adapter import JinaConfig, JinaCookie, JinaCredentials
from mato_loop_backend.permissions.policy import PermissionDecision
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings import (
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
    ToolExecutionPolicy,
    ToolSettings,
)
from mato_loop_backend.tools import access
from mato_loop_backend.tools import web as web_tools
from mato_loop_backend.tools.arguments import JinaWebFetchArguments
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.web.fetch import WebFetchError, WebFetchResult


@pytest.fixture(autouse=True)
def user_language(monkeypatch):
    monkeypatch.setattr(web_tools, "preferred_language", AsyncMock(return_value=None))


def _transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))


@pytest.mark.parametrize("format", ["markdown", "text", "html"])
async def test_reader_formats_keep_sources_and_bound_credentials(monkeypatch, format):
    def respond(request):
        assert str(request.url) == "https://reader.test/"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content)["url"] == "https://target.test/page#route"
        assert request.headers["X-Respond-With"] == format
        assert (request.headers["X-Timeout"], request.extensions["timeout"]["read"]) == ("80", 90)
        assert {key: request.headers[key] for key in (
            "X-Engine", "X-Remove-Overlay", "X-No-Cache", "X-Locale", "X-Target-Selector", "X-Wait-For-Selector", "X-Remove-Selector",
        )} == {"X-Engine": "browser", "X-Remove-Overlay": "false", "X-No-Cache": "true", "X-Locale": "de-DE", "X-Target-Selector": r".caf\e9 ", "X-Wait-For-Selector": ".ready", "X-Remove-Selector": "nav"}
        body = json.dumps({"code": 200, "data": {
            "url": "https://target.test/final", "title": " Page ",
            "content": "<script>bad()</script><b>Article</b>" if format == "html" else "Article",
            "links": {"Article": "https://target.test/article", "Unsafe": "javascript:bad()"},
            "images": {"Photo": "https://target.test/photo.jpg"},
        }}).encode()
        return httpx.Response(200, stream=httpx.ByteStream(body))

    _transport(monkeypatch, respond)
    result = await client.fetch(JinaConfig(base_url="https://reader.test", remove_overlay=True),
                                "https://target.test/page#route", api_key="test-key", format=format,
                                engine="browser", remove_overlay=False, no_cache=True, locale="de-DE",
                                target_selector=".café", wait_for_selector=".ready", remove_selector="nav",
                                with_links_summary=True, with_images_summary=True)
    assert (result.content, result.final_url, result.title) == (
        "<b>Article</b>" if format == "html" else "Article", "https://target.test/final", "Page",
    )
    assert (result.links, result.image_links) == ((("Article", "https://target.test/article"),), (("Photo", "https://target.test/photo.jpg"),))


@pytest.mark.parametrize(("status", "body", "expected"), [
    (429, b"private upstream details", "integration.rateLimited"),
    (200, b"x" * 1001, "webfetch.responseTooLarge"),
    (200, b"not json", "integration.invalidResponse"),
    (200, json.dumps({"code": 200, "data": {
        "url": "https://target.test", "content": "Cloudflare security check",
        "warning": "Target URL returned error 403: Forbidden\nThis page maybe requiring CAPTCHA.",
    }}).encode(), "webfetch.requestFailed"),
])
async def test_reader_failures_are_bounded_and_safe(monkeypatch, status, body, expected):
    _transport(monkeypatch, lambda _request: httpx.Response(status, stream=httpx.ByteStream(body)))
    with pytest.raises(WebFetchError) as failure:
        await client.fetch(JinaConfig(base_url="https://reader.test"), "https://target.test", max_response_bytes=1000)
    assert str(failure.value) == expected


async def test_reader_deadline_includes_the_request(monkeypatch):
    real_timeout = asyncio.timeout
    deadline = Mock(side_effect=lambda _seconds: real_timeout(0.01))
    monkeypatch.setattr(client.asyncio, "timeout", deadline)

    async def slow(_request):
        await asyncio.sleep(1)
        return httpx.Response(200)

    _transport(monkeypatch, slow)
    with pytest.raises(WebFetchError, match="webfetch.timeout"):
        await client.fetch(JinaConfig(base_url="https://reader.test"), "https://target.test")
    deadline.assert_called_once_with(90)


async def test_instance_credentials_can_be_kept_and_removed(monkeypatch):
    settings = IntegrationSettings()
    db = AsyncMock()
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, model, **_kwargs: settings if model is IntegrationSettings else ToolSettings()))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    store = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    delete = AsyncMock()
    monkeypatch.setattr(service, "store_credential", store)
    monkeypatch.setattr(service, "delete_credential", delete)
    monkeypatch.setattr(service, "credentials", AsyncMock(return_value=JinaCredentials()))
    values = {"name": "Reader", "base_url": "https://reader.test", "enabled": True}
    await service.JinaIntegrationService.save(db, None, api_key="secret", **values)
    instance = settings.instances[0]
    credential_id = instance.credential_id
    await service.JinaIntegrationService.save(db, instance.id, api_key=None, **values)
    assert instance.credential_id == credential_id
    store.assert_awaited_once_with(db, owner_id=None, kind="jina.api_key", label="Reader", payload={"api_key": "secret"})
    await service.JinaIntegrationService.save(db, instance.id, api_key="", **values)
    assert instance.credential_id is None
    delete.assert_awaited_once_with(db, credential_id, owner_id=None)


@pytest.mark.parametrize("url", ["https://target.test/page", "https://sub.target.test/", "https://eviltarget.test/", "http://target.test/"])
async def test_cookies_are_host_only_secure_and_use_the_browser(monkeypatch, url):
    requests = []
    _transport(monkeypatch, lambda request: (requests.append(request.headers) or httpx.Response(
        200, stream=httpx.ByteStream(json.dumps({"code": 200, "data": {"url": url, "content": "Page"}}).encode()),
    )))
    await client.fetch(JinaConfig(base_url="https://reader.test"), url,
                       cookies=[JinaCookie(domain="target.test", name="consent", value="yes")])
    assert (requests[0].get("X-Set-Cookie"), requests[0]["X-Engine"]) == (
        ("consent=yes; Path=/; Secure", "browser") if url == "https://target.test/page" else (None, "auto")
    )


@pytest.mark.parametrize(("engine", "value", "expected"), [
    ("curl", "yes", "tool.invalidArguments"),
    ("auto", "probe+a/b==%26", "integration.invalidConfiguration"),
])
async def test_unsupported_cookie_requests_fail_before_network(monkeypatch, engine, value, expected):
    fetch = Mock()
    monkeypatch.setattr(client.httpx, "AsyncClient", fetch)
    with pytest.raises(WebFetchError, match=expected):
        await client.fetch(JinaConfig(base_url="https://reader.test"), "https://target.test", engine=engine,
                           cookies=[JinaCookie(domain="target.test", name="consent", value=value)])
    fetch.assert_not_called()


@pytest.mark.parametrize(("field", "value"), [("domain", "*.target.test"), ("domain", "target..test"), ("name", "x; Domain=evil.test"), ("value", "yes; Domain=evil.test"), ("value", "yes\r\nInjected: x")])
def test_cookie_scope_and_header_injection_are_rejected(field, value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        JinaCookie.model_validate({"domain": "target.test", "name": "consent", "value": "yes", field: value})


async def test_cookie_replacement_preserves_api_key_and_keeps_secrets_out_of_configuration(monkeypatch):
    instance = IntegrationInstance(name="Reader", integration_type="webfetch", adapter_type="jina", credential_id=uuid4())
    settings = IntegrationSettings(instances=[instance])
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, model, **_kwargs: settings if model is IntegrationSettings else ToolSettings()))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    store = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(service, "store_credential", store)
    monkeypatch.setattr(service, "delete_credential", AsyncMock())
    monkeypatch.setattr(service, "credentials", AsyncMock(return_value=JinaCredentials(
        api_key="existing-key", cookies=[JinaCookie(domain="target.test", name="consent", value="old+cookie==")],
    )))
    await service.JinaIntegrationService.save(AsyncMock(), instance.id, name="Reader", base_url="https://reader.test",
        enabled=True, api_key=None, cookies=[{"domain": "target.test", "name": "consent", "value": "private-cookie"}])
    assert (store.await_args.kwargs["payload"], settings.instances[0].config) == (
        {"api_key": "existing-key", "cookies": [{"domain": "target.test", "name": "consent", "value": "private-cookie"}]},
        {"base_url": "https://reader.test/", "timeout_seconds": 90, "remove_overlay": False, "cookie_domains": ["target.test"], "has_api_key": True},
    )


async def test_cookie_values_changed_by_reader_cannot_be_saved():
    from mato_loop_backend.integrations.service import IntegrationSettingsError

    with pytest.raises(IntegrationSettingsError, match="validation.invalid") as failure:
        await service.JinaIntegrationService.save(AsyncMock(), None, name="Reader", base_url="https://reader.test",
            enabled=True, api_key=None, cookies=[{"domain": "target.test", "name": "consent", "value": "probe+a/b==%26"}])
    assert failure.value.field == "cookies"


@pytest.mark.parametrize("jina", [False, True])
def test_webfetch_schema_uses_reader_arguments_and_description_only_for_jina(jina):
    provider = IntegrationInstance(name="Reader", integration_type="webfetch", adapter_type="jina", config={"base_url": "https://reader.test"})
    settings = IntegrationSettings(instances=[provider], webfetch_integration_id=provider.id if jina else None)
    schema = tool_registry.effective(settings, webfetch_enabled=True).tools["webfetch"].schema()
    assert ("engine" in schema["parameters"]["properties"], "through Jina Reader" in schema["description"]) == (jina, jina)


@pytest.mark.parametrize("options", [{"pdf_pages": [1], "engine": "browser"}, {"target_selector": "main\r\nInjected: yes"}])
def test_reader_options_reject_direct_pdf_conflicts_and_header_injection(options):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        JinaWebFetchArguments(url="https://target.test", **options)


@pytest.mark.parametrize("state", ["allowed", "denied", "changed", "excluded", "changed_before_execution"])
async def test_webfetch_uses_only_the_selected_current_reader(tmp_path, monkeypatch, state):
    call = ToolCall(id="read", name="webfetch", arguments={"url": "http://internal.test/page"})
    context = _context(tmp_path, call, web_fetch_policy=ToolExecutionPolicy.ASK)
    provider = IntegrationInstance(name="Reader", integration_type="webfetch", adapter_type="jina",
                                   config={"base_url": "https://reader.test"})
    settings = IntegrationSettings(instances=[provider], webfetch_integration_id=provider.id)
    tools = tool_registry.effective(settings, webfetch_enabled=True, tool_settings=context.tool_settings)
    if state == "changed_before_execution":
        settings.instances[0].config["base_url"] = "https://other.test"
    current = settings.model_copy(deep=True)
    if state == "changed":
        current.instances[0].config["base_url"] = "https://other.test"
    if state == "excluded":
        settings.instances = []
    monkeypatch.setattr(type(context), "current_integrations", AsyncMock(side_effect=[settings, current]))
    monkeypatch.setattr(access, "profile_tool_settings", AsyncMock(return_value=context.tool_settings))
    context.ask_handler.return_value = PermissionDecision.DENY if state == "denied" else PermissionDecision.ALLOW
    fetch = AsyncMock(return_value=WebFetchResult("http://internal.test/page", "text/markdown", "Article", 200, "markdown"))
    monkeypatch.setattr(client, "fetch", fetch)
    result = await tools.execute(call, context)
    if state == "allowed":
        assert json.loads(result.content)["content"] == "Article"
    else:
        fetch.assert_not_awaited()
        assert result.metadata["errorCode"] == ("permission.denied" if state == "denied" else "tool.unavailable")


@pytest.mark.parametrize("primary", ["builtin", "jina"])
async def test_webfetch_ignores_legacy_fallback_on_provider_failure(tmp_path, monkeypatch, primary):
    call = ToolCall("fetch", "webfetch", {"url": "https://target.test"})
    context = _context(tmp_path, call, web_fetch_policy=ToolExecutionPolicy.AUTO)
    provider = IntegrationInstance(name="Reader", integration_type="webfetch", adapter_type="jina", config={"base_url": "https://reader.test"})
    settings = IntegrationSettings(
        instances=[provider], webfetch_integration_id=provider.id if primary == "jina" else None,
        webfetch_fallback_provider="builtin" if primary == "jina" else f"jina:{provider.id}",
    )
    monkeypatch.setattr(type(context), "current_integrations", AsyncMock(return_value=settings))
    monkeypatch.setattr(access, "profile_tool_settings", AsyncMock(return_value=context.tool_settings))
    first = AsyncMock(side_effect=WebFetchError("webfetch.timeout"))
    fallback = AsyncMock(return_value=WebFetchResult("https://target.test/", "text/plain", "Article", 200, "text"))
    monkeypatch.setattr(client, "fetch", first if primary == "jina" else fallback)
    monkeypatch.setattr(web_tools, "fetch_web", first if primary == "builtin" else fallback)

    result = await tool_registry.effective(settings, webfetch_enabled=True, tool_settings=context.tool_settings).execute(call, context)

    assert (fallback.await_count, result.metadata["errorCode"]) == (0, "webfetch.timeout")
