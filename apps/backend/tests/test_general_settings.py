from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.routes import general_settings, mcp
from mato_loop_backend.api.routes.tool_settings import (
    ToolConfigurationRequest,
    tool_configuration_payload,
    update_tool_configuration,
)
from mato_loop_backend.permissions.resolution import tool_execution_policy
from mato_loop_backend.public_url import resolve_public_base_url
from mato_loop_backend.settings import (
    GeneralSettings,
    IntegrationSettings,
    SettingsStore,
    ToolExecutionPolicy,
    ToolSettings,
    TrustLevel,
)


@pytest.fixture
def http_request():
    return Request(
        {
            "type": "http",
            "scheme": "https",
            "path": "/",
            "headers": [
                (b"host", b"mato.example.test"),
                (b"x-forwarded-host", b"untrusted.example.test"),
            ],
        }
    )


@pytest.mark.parametrize(
    "stored, environment, expected, source",
    [
        (
            "https://stored.example.test",
            "https://env.example.test",
            "https://stored.example.test",
            "settings",
        ),
        (None, "https://env.example.test/", "https://env.example.test", "environment"),
        (None, None, "https://mato.example.test", "automatic"),
    ],
)
def test_public_url_priority_and_server_detection(
    monkeypatch, http_request, stored, environment, expected, source
):
    monkeypatch.setattr(
        "mato_loop_backend.public_url.get_runtime_config",
        lambda: SimpleNamespace(public_base_url=environment),
    )
    assert resolve_public_base_url(stored, http_request) == (expected, source)


@pytest.mark.parametrize("base", ["https://public.example.test", "http://192.168.1.30:10020"])
async def test_admin_public_url_is_saved_and_used_by_next_oauth_login(monkeypatch, http_request, base):
    settings = GeneralSettings(default_timezone="UTC")
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, kind, **_: settings if kind is GeneralSettings else kind()))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    db = AsyncMock()
    await general_settings.update_general_settings(
        general_settings.GeneralSettingsUpdate(publicBaseUrl=f" {base}/ "), http_request, db
    )
    assert (
        await mcp._callback_uri(db, http_request)
        == base + "/api/mcp/oauth/callback"
    )
    assert settings.default_timezone == "UTC"
    db.commit.assert_awaited_once()


async def test_model_trust_switch_preserves_stored_general_settings(monkeypatch, http_request):
    settings = GeneralSettings(default_timezone="UTC", publicBaseUrl="https://public.example.test")
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, kind, **_: settings if kind is GeneralSettings else kind()))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    result = await general_settings.update_general_settings(
        general_settings.GeneralSettingsUpdate(modelTrustEnabled=False), http_request, AsyncMock()
    )
    assert (result["modelTrustEnabled"], settings.model_trust_enabled, settings.default_timezone, settings.public_base_url) == (False, False, "UTC", "https://public.example.test")


def tool_request(settings, **updates):
    payload = tool_configuration_payload(settings)
    for key in ("groups", "privateGroups", "publicGroups", "toolPermissionPresets", "presetOptions", "webSearchProviderOptions", "webfetchProviderOptions"):
        payload.pop(key)
    return ToolConfigurationRequest.model_validate({**payload, **updates})


@pytest.mark.parametrize("trust", ["private", "public"])
@pytest.mark.parametrize("preset, read, write, delete, sensitive", [
    ("ask_all", "ask", "ask", "ask", "ask"),
    ("balanced", "ask", "ask", "ask", "ask"),
    ("ask_changes", "auto", "ask", "ask", "auto"),
    ("ask_delete", "auto", "auto", "ask", "auto"),
    ("auto_all", "auto", "auto", "auto", "auto"),
])
async def test_tool_presets_change_only_selected_global_policies(monkeypatch, trust, preset, read, write, delete, sensitive):
    tools = ToolSettings(
        private_permissions={"policies": {"filesystem.delete": "disabled"}},
        public_permissions={"policies": {"filesystem.delete": "disabled"}},
        tool_availability={"shell_run": False}, shell_prefixes={"read": [["git", "status"]]},
    )
    before = tools.model_dump()
    documents = {GeneralSettings: GeneralSettings(), ToolSettings: tools, IntegrationSettings: IntegrationSettings()}
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, kind, **_: documents[kind]))
    save = AsyncMock()
    monkeypatch.setattr(SettingsStore, "save", save)
    db = AsyncMock()
    result = await update_tool_configuration(
        tool_request(tools, toolPermissionPresets={trust: preset}), SimpleNamespace(is_admin=True), db
    )
    tools.select_trust(TrustLevel(trust))
    policies = getattr(tools, f"{trust}_permissions").policies
    assert tuple(tool_execution_policy(tools, key) for key in (
        "filesystem.read", "filesystem.write", "shell.write", "filesystem.delete", "automation.delete",
        "sensitive_data.access", "secrets.access", "secrets.write",
    )) == (read, write, write, "disabled", delete, sensitive, "ask", write)
    for key in (
        "network.search", "network.fetch", "session.files.read", "session.files.write", "file.provide",
        "media.generate.image", "media.edit.image", "media.generate.speech", "media.generate.audio",
        "media.transcribe.audio", "media.analyze.audio", "media.analyze.video", "skills.load", "session.tool_result.read",
    ):
        assert tool_execution_policy(tools, key) == ("ask" if preset == "ask_all" else "auto")
    selected = next(option for option in result["presetOptions"] if option["value"] == preset)
    assert [rule["policy"] for rule in selected["baseline"]] == [read, sensitive, write, delete]
    assert selected["additionalConfirmations"] == (["Variablen und Secrets verwenden"] if sensitive == "auto" else []) + (["Browser verwenden (außer im Build-Profil)", "Browser-Python ausführen"] if write == "auto" else [])
    after = tools.model_dump()
    after[f"{trust}_permission_preset"] = before[f"{trust}_permission_preset"]
    assert after == before
    assert result["toolPermissionPresets"][trust] == preset
    assert {type(call.args[1]) for call in save.await_args_list} == {ToolSettings, IntegrationSettings}
    db.commit.assert_awaited_once()

    policies["filesystem.read"] = ToolExecutionPolicy.DISABLED
    result = await update_tool_configuration(tool_request(tools), SimpleNamespace(is_admin=True), db)
    assert result["toolPermissionPresets"][trust] == preset
    getattr(tools, f"{trust}_permissions").policies.pop("filesystem.read")
    assert tool_execution_policy(tools, "filesystem.read") == read
    assert len(save.await_args_list) == 4


async def test_disabled_model_distinction_preserves_public_tool_permissions(monkeypatch):
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, kind, **_: GeneralSettings(modelTrustEnabled=False) if kind is GeneralSettings else kind()))
    with pytest.raises(ApiError, match="validation.invalidChoice"):
        await update_tool_configuration(
            tool_request(ToolSettings(), toolPermissionPresets={"public": "auto_all"}), SimpleNamespace(is_admin=True), AsyncMock()
        )


@pytest.mark.parametrize("availability,invalid", [
    ({"shell_run": False}, False),
    ({"shell_run": False, "browser": False}, False),
    ({"browser": True}, True),
    ({"unknown": False}, True),
])
async def test_global_tool_saves_preserve_unsubmitted_configuration(monkeypatch, availability, invalid):
    tools = ToolSettings(tool_availability={"browser": False}, web_access={"userAgent": "Shared/2.0", "maxResponseMiB": 7, "publicAddressesOnly": False})
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, kind, **_: tools if kind is ToolSettings else kind()))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    request = ToolConfigurationRequest.model_validate(tool_request(tools, toolAvailability=availability).model_dump(by_alias=True, exclude={"web_access"}))
    if invalid:
        with pytest.raises(ApiError, match="validation.invalidChoice"):
            await update_tool_configuration(request, SimpleNamespace(is_admin=True), AsyncMock())
    else:
        result = await update_tool_configuration(request, SimpleNamespace(is_admin=True), AsyncMock())
        assert result["toolAvailability"] == {"browser": False, "shell_run": False}
        assert result["webAccess"] == {"userAgent": "Shared/2.0", "maxResponseMiB": 7, "publicAddressesOnly": False}


@pytest.mark.parametrize("presets", [{"other": "auto_all"}, {"private": "unknown"}, {"private": None}])
def test_tool_presets_validate_requested_branch_and_preset(presets):
    with pytest.raises(ValidationError):
        tool_request(ToolSettings(), toolPermissionPresets=presets)


def test_general_settings_are_admin_only():
    assert all(
        any(
            dependency.call.__name__ == "require_admin"
            for dependency in route.dependant.dependencies
        )
        for route in general_settings.router.routes
    )


def test_changing_live_preset_cannot_widen_frozen_delegation():
    from mato_loop_backend.agent.subagents import apply_delegation_ceiling, permission_snapshot

    tools = ToolSettings(public_permission_preset="ask_all").select_trust(TrustLevel.PUBLIC)
    snapshot = permission_snapshot(tools, {"read"})
    tools.public_permission_preset = "auto_all"
    apply_delegation_ceiling(tools, snapshot)
    assert tool_execution_policy(tools, "filesystem.read") == ToolExecutionPolicy.ASK


async def test_auto_update_default_triggers_connected_agents_only_on_change(monkeypatch, http_request):
    settings = GeneralSettings()
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    schedule = Mock()
    monkeypatch.setattr(general_settings, "schedule_auto_updates", schedule)
    db = AsyncMock()
    result = await general_settings.update_general_settings(general_settings.GeneralSettingsUpdate(agentAutoUpdateEnabled=True), http_request, db)
    await general_settings.update_general_settings(general_settings.GeneralSettingsUpdate(modelTrustEnabled=False), http_request, db)
    assert result["agentAutoUpdateEnabled"] is True
    schedule.assert_called_once_with()
