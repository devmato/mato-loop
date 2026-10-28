from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.routes.tool_settings import (
    MediaTimeoutRequest,
    get_tool_availability,
    get_tool_configuration,
    tool_configuration_payload,
)
from mato_loop_backend.permissions.resolution import resolve_tool_execution_policy
from mato_loop_backend.settings import (
    AgentProfilePermissions,
    IntegrationSettings,
    SettingsStore,
    ToolExecutionPolicy,
    ToolSettings,
    TrustLevel,
    WebAccessSettings,
    build_profile,
    core_profiles,
    plan_profile,
    sysadmin_profile,
)
from mato_loop_backend.tools.catalog import tool_registry


def _db() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


async def test_nonadmin_cannot_read_tool_configuration(monkeypatch) -> None:
    load = AsyncMock(return_value=ToolSettings())
    monkeypatch.setattr(SettingsStore, "load", load)

    with pytest.raises(ApiError, match="auth.adminRequired"):
        await get_tool_configuration(SimpleNamespace(is_admin=False), _db())

    load.assert_not_awaited()


def test_legacy_duckduckgo_provider_is_migrated_to_no_key_search() -> None:
    settings = ToolSettings.model_validate(
        {"webSearch": {"primaryProvider": "duckduckgo"}}
    )

    assert settings.web_search.primary_provider == "no_key"


@pytest.mark.parametrize("user_agent", [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Custom-Agent/2.0",
])
def test_web_access_preserves_explicit_user_agents(user_agent) -> None:
    settings = WebAccessSettings.model_validate({"userAgent": user_agent})
    assert settings.user_agent == user_agent


@pytest.mark.parametrize(
    ("session_policy", "workspace_policy", "available"),
    [
        (ToolExecutionPolicy.DISABLED, ToolExecutionPolicy.ASK, True),
        (ToolExecutionPolicy.AUTO, ToolExecutionPolicy.DISABLED, True),
        (ToolExecutionPolicy.DISABLED, ToolExecutionPolicy.DISABLED, False),
    ],
)
def test_root_specific_write_policies_keep_shared_tools_available(
    session_policy: ToolExecutionPolicy,
    workspace_policy: ToolExecutionPolicy,
    available: bool,
) -> None:
    settings = ToolSettings(
        public_permissions={
            "policies": {
                "session.files.write": session_policy,
                "filesystem.write": workspace_policy,
            }
        }
    ).select_trust(TrustLevel.PUBLIC)

    tools = tool_registry.effective(
        IntegrationSettings(), tool_settings=settings
    ).tools

    assert ("write_file" in tools) is available
    assert ("create_directory" in tools) is available
    assert ("copy_path" in tools) is available
    assert ("move_path" in tools) is available


@pytest.mark.parametrize(
    ("disabled", "read_available", "copy_available", "move_available"),
    [
        ((), True, True, True),
        (("filesystem.read", "filesystem.write", "filesystem.delete"), True, True, True),
        (("session.files.read", "session.files.write"), True, True, True),
        (("session.files.read", "filesystem.write"), True, True, True),
        (("filesystem.read", "session.files.write"), True, True, True),
        (("session.files.write", "filesystem.write"), True, False, False),
        (("session.files.read", "filesystem.read"), False, False, True),
        (("session.files.write", "filesystem.delete"), True, True, False),
    ],
)
def test_root_specific_read_and_transfer_availability(
    disabled: tuple[str, ...],
    read_available: bool,
    copy_available: bool,
    move_available: bool,
) -> None:
    settings = ToolSettings(
        public_permissions={
            "policies": {
                capability: (
                    ToolExecutionPolicy.DISABLED
                    if capability in disabled
                    else ToolExecutionPolicy.AUTO
                )
                for capability in (
                    "session.files.read",
                    "filesystem.read",
                    "session.files.write",
                    "filesystem.write",
                    "filesystem.delete",
                )
            }
        }
    ).select_trust(TrustLevel.PUBLIC)

    tools = tool_registry.effective(
        IntegrationSettings(), tool_settings=settings
    ).tools

    for name in ("read", "glob", "grep"):
        assert (name in tools) is read_available
    assert "provide_file" in tools
    assert ("copy_path" in tools) is copy_available
    assert ("move_path" in tools) is move_available
    assert "delete_path" in tools


def test_external_working_directory_policy_does_not_hide_directory_tool() -> None:
    settings = ToolSettings(
        public_permissions={
            "policies": {"working_directory.external": ToolExecutionPolicy.DISABLED}
        }
    ).select_trust(TrustLevel.PUBLIC)

    tools = tool_registry.effective(
        IntegrationSettings(),
        tool_settings=settings,
        execution_operations={"filesystem.resolve"},
    ).tools

    assert "change_directory" in tools


@pytest.mark.parametrize("trust", [TrustLevel.PRIVATE, TrustLevel.PUBLIC])
@pytest.mark.parametrize(
    "capability",
    [
        "session.files.read",
        "session.files.write",
        "session.files.delete",
        "network.fetch",
        "network.search",
        "memory.read",
        "skills.load",
        "media.generate.image",
        "secrets.access",
    ],
)
def test_category_defaults_and_saved_policies(
    trust: TrustLevel, capability: str
) -> None:
    settings = ToolSettings().select_trust(trust)
    expected = (
        ToolExecutionPolicy.ASK if capability in {"session.files.delete", "secrets.access"} or (trust == TrustLevel.PUBLIC and capability == "memory.read") else ToolExecutionPolicy.AUTO
    )
    assert resolve_tool_execution_policy(settings, capability).policy == expected
    for policy in ToolExecutionPolicy:
        settings.permissions.policies[capability] = policy
        assert resolve_tool_execution_policy(settings, capability).policy == policy


@pytest.mark.parametrize("trust", [TrustLevel.PRIVATE, TrustLevel.PUBLIC])
@pytest.mark.parametrize(
    ("global_policies", "profile_policies", "expected"),
    [
        ({"filesystem.read": "ask"}, {}, None),
        ({"filesystem.read": "disabled"}, {}, None),
        ({"filesystem.read": "auto"}, {}, None),
        ({"filesystem.read": "disabled", "session.files.read": "auto"}, {}, "auto"),
        ({"filesystem.read": "auto", "session.files.read": "disabled"}, {}, "disabled"),
        ({}, {"filesystem.read": "disabled"}, "disabled"),
        ({"session.files.read": "disabled"}, {"filesystem.read": "auto"}, "auto"),
        ({"session.files.read": "auto"}, {"filesystem.read": "ask"}, "ask"),
        ({}, {"filesystem.read": "disabled", "session.files.read": "auto"}, "auto"),
        ({}, {"filesystem.read": "auto", "session.files.read": "disabled"}, "disabled"),
    ],
)
def test_session_file_read_uses_own_global_override_and_profile_rules(
    trust: TrustLevel,
    global_policies: dict[str, str],
    profile_policies: dict[str, str],
    expected: str | None,
) -> None:
    settings = ToolSettings(
        private_permissions={"policies": global_policies},
        public_permissions={"policies": global_policies},
    ).select_trust(trust)
    settings._agent_permissions = AgentProfilePermissions(policies=profile_policies)

    assert (
        resolve_tool_execution_policy(
            settings, "session.files.read", ToolExecutionPolicy.AUTO
        ).policy
        == (expected or "auto")
    )


@pytest.mark.parametrize(
    ("baseline", "profile", "expected"),
    [
        (ToolExecutionPolicy.ASK, ToolExecutionPolicy.AUTO, ToolExecutionPolicy.AUTO),
        (ToolExecutionPolicy.AUTO, ToolExecutionPolicy.ASK, ToolExecutionPolicy.ASK),
        (ToolExecutionPolicy.ASK, ToolExecutionPolicy.DISABLED, ToolExecutionPolicy.DISABLED),
        (ToolExecutionPolicy.DISABLED, ToolExecutionPolicy.AUTO, ToolExecutionPolicy.AUTO),
    ],
)
def test_agent_profile_policy_controls_available_global_capability(
    baseline: ToolExecutionPolicy,
    profile: ToolExecutionPolicy,
    expected: ToolExecutionPolicy,
) -> None:
    settings = ToolSettings(
        public_permissions={"policies": {"filesystem.read": baseline}}
    ).select_trust(TrustLevel.PUBLIC)
    settings._agent_permissions = AgentProfilePermissions(
        policies={"filesystem.read": profile}
    )

    result = resolve_tool_execution_policy(
        settings, "filesystem.read", ToolExecutionPolicy.AUTO
    )

    assert result.policy == expected


def test_agent_profile_missing_capability_inherits_live_default() -> None:
    settings = ToolSettings(
        public_permissions={"policies": {"filesystem.read": ToolExecutionPolicy.AUTO}}
    ).select_trust(TrustLevel.PUBLIC)
    settings._agent_permissions = AgentProfilePermissions()

    assert (
        resolve_tool_execution_policy(settings, "filesystem.read").policy
        == ToolExecutionPolicy.AUTO
    )


@pytest.mark.parametrize("trust", [TrustLevel.PRIVATE, TrustLevel.PUBLIC])
def test_plan_profile_asks_for_changes(trust: TrustLevel) -> None:
    profile = plan_profile()
    settings = ToolSettings().select_trust(trust)
    settings._agent_permissions = (
        profile.private_permissions
        if trust == TrustLevel.PRIVATE
        else profile.public_permissions
    )

    for capability in (
        "filesystem.write",
        "filesystem.delete",
        "shell.write",
        "shell.delete",
        "session.files.write",
        "session.files.delete",
    ):
        assert (
            resolve_tool_execution_policy(settings, capability).policy
            == ToolExecutionPolicy.ASK
        )


@pytest.mark.parametrize("trust", [TrustLevel.PRIVATE, TrustLevel.PUBLIC])
def test_only_coding_profiles_default_to_automatic_public_computer_reads(trust) -> None:
    for profile in core_profiles():
        settings = ToolSettings().select_trust(trust)
        settings._agent_permissions = (
            profile.private_permissions if trust == TrustLevel.PRIVATE else profile.public_permissions
        )
        for capability in ("filesystem.read", "shell.read"):
            expected = (
                ToolExecutionPolicy.ASK
                if profile.name == "Allgemein" and trust == TrustLevel.PUBLIC
                else ToolExecutionPolicy.AUTO
            )
            assert resolve_tool_execution_policy(
                settings, capability
            ).policy == expected
            settings._agent_permissions.policies[capability] = ToolExecutionPolicy.ASK
            assert resolve_tool_execution_policy(
                settings, capability
            ).policy == ToolExecutionPolicy.ASK


@pytest.mark.parametrize("trust", [TrustLevel.PRIVATE, TrustLevel.PUBLIC])
def test_build_profile_defaults_to_auto_writes_and_preserves_overrides(trust: TrustLevel) -> None:
    profile = build_profile()
    settings = ToolSettings().select_trust(trust)
    settings._agent_permissions = (
        profile.private_permissions
        if trust == TrustLevel.PRIVATE
        else profile.public_permissions
    )

    for capability in ("filesystem.write", "filesystem.delete", "shell.write"):
        assert (
            resolve_tool_execution_policy(settings, capability).policy
            == ToolExecutionPolicy.AUTO
        )
        for policy in (ToolExecutionPolicy.ASK, ToolExecutionPolicy.DISABLED):
            settings._agent_permissions.policies[capability] = policy
            assert resolve_tool_execution_policy(settings, capability).policy == policy
    assert (
        resolve_tool_execution_policy(
            settings,
            "calendar.write",
            ToolExecutionPolicy.ASK,
            integration_id=uuid4(),
        ).policy
        == ToolExecutionPolicy.ASK
    )


@pytest.mark.parametrize("trust", [TrustLevel.PRIVATE, TrustLevel.PUBLIC])
def test_sysadmin_profile_reads_automatically_and_asks_for_changes(trust: TrustLevel) -> None:
    profile = sysadmin_profile()
    settings = ToolSettings(private_permission_preset="ask_all", public_permission_preset="ask_all").select_trust(trust)
    settings._agent_permissions = (
        profile.private_permissions
        if trust == TrustLevel.PRIVATE
        else profile.public_permissions
    )

    for capability in ("filesystem.read", "shell.read", "working_directory.external"):
        assert resolve_tool_execution_policy(settings, capability).policy == ToolExecutionPolicy.AUTO
    for capability in ("filesystem.write", "filesystem.delete", "shell.write", "sensitive_data.access", "secrets.access"):
        assert resolve_tool_execution_policy(settings, capability).policy == ToolExecutionPolicy.ASK


def test_agent_profile_overrides_scoped_defaults_but_not_hard_limits() -> None:
    integration_id = uuid4()
    settings = ToolSettings(
        public_permissions={
            "policies": {"calendar.write": ToolExecutionPolicy.ASK},
            "integrationPolicies": {
                str(integration_id): {"calendar.write": ToolExecutionPolicy.DISABLED}
            },
        },
    ).select_trust(TrustLevel.PUBLIC)
    settings._agent_permissions = AgentProfilePermissions(
        integration_policies={
            str(integration_id): {"calendar.write": ToolExecutionPolicy.AUTO}
        }
    )

    assert (
        resolve_tool_execution_policy(
            settings, "calendar.write", integration_id=integration_id
        ).policy
        == ToolExecutionPolicy.AUTO
    )

    settings.permissions.policies["calendar.write"] = ToolExecutionPolicy.DISABLED
    assert (
        resolve_tool_execution_policy(
            settings, "calendar.write", integration_id=integration_id
        ).policy
        == ToolExecutionPolicy.AUTO
    )


def test_agent_profile_inherits_live_resource_default() -> None:
    integration_id = uuid4()
    resource_key = "calendar:work"
    settings = ToolSettings(
        public_permissions={
            "policies": {"calendar.read": ToolExecutionPolicy.ASK},
            "resourcePolicies": {
                str(integration_id): {
                    resource_key: {"calendar.read": ToolExecutionPolicy.DISABLED}
                }
            },
        },
    ).select_trust(TrustLevel.PUBLIC)
    settings._agent_permissions = AgentProfilePermissions()

    assert (
        resolve_tool_execution_policy(
            settings,
            "calendar.read",
            integration_id=integration_id,
            resource_key=resource_key,
        ).policy
        == ToolExecutionPolicy.DISABLED
    )

    settings.permissions.resource_policies[str(integration_id)][resource_key][
        "calendar.read"
    ] = ToolExecutionPolicy.AUTO
    assert (
        resolve_tool_execution_policy(
            settings,
            "calendar.read",
            integration_id=integration_id,
            resource_key=resource_key,
        ).policy
        == ToolExecutionPolicy.AUTO
    )


def test_unavailable_agent_profile_fails_closed() -> None:
    settings = ToolSettings(
        public_permissions={"policies": {"filesystem.read": ToolExecutionPolicy.AUTO}}
    ).select_trust(TrustLevel.PUBLIC)
    settings._agent_profile_unavailable = True

    assert (
        resolve_tool_execution_policy(settings, "filesystem.read").policy
        == ToolExecutionPolicy.DISABLED
    )


def test_agent_profile_scoped_capability_is_offered_and_resolved_for_its_target() -> None:
    integration_id = uuid4()
    settings = ToolSettings(
        public_permissions={"policies": {"mail.read": ToolExecutionPolicy.AUTO}}
    ).select_trust(TrustLevel.PUBLIC)
    settings._agent_permissions = AgentProfilePermissions(
        policies={"mail.read": ToolExecutionPolicy.DISABLED},
        integration_policies={str(integration_id): {"mail.read": ToolExecutionPolicy.ASK}},
    )

    assert (
        resolve_tool_execution_policy(settings, "mail.read").policy
        == ToolExecutionPolicy.DISABLED
    )
    assert (
        resolve_tool_execution_policy(
            settings, "mail.read", include_scoped=True
        ).policy
        == ToolExecutionPolicy.ASK
    )
    assert (
        resolve_tool_execution_policy(
            settings, "mail.read", integration_id=integration_id
        ).policy
        == ToolExecutionPolicy.ASK
    )
    assert (
        resolve_tool_execution_policy(
            settings, "mail.read", integration_id=uuid4()
        ).policy
        == ToolExecutionPolicy.ASK
    )


@pytest.mark.parametrize("capability", ["network.fetch", "network.search"])
@pytest.mark.parametrize("policy", list(ToolExecutionPolicy))
def test_agent_profile_can_override_web_capability(capability, policy) -> None:
    settings = ToolSettings(
        public_permissions={"policies": {capability: "disabled"}}
    ).select_trust(TrustLevel.PUBLIC)
    settings._agent_permissions = AgentProfilePermissions(
        policies={capability: policy}
    )

    assert (
        resolve_tool_execution_policy(settings, capability).policy
        == policy
    )


async def test_get_tool_configuration_returns_defaults(monkeypatch) -> None:
    settings = ToolSettings()
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))

    result = await get_tool_configuration(
        SimpleNamespace(is_admin=True), _db()
    )

    assert {
        key: result[key]
        for key in (
            "webAccess",
            "generateImage",
            "editImage",
            "generateSpeech",
            "generateAudio",
            "transcribeAudio",
            "analyzeVideo",
        )
    } == {
        "webAccess": {
            "userAgent": "MatoLoop/1.0",
            "maxResponseMiB": 64,
            "publicAddressesOnly": True,
        },
        "generateImage": {"timeoutSeconds": 300},
        "editImage": {"timeoutSeconds": 300},
        "generateSpeech": {"timeoutSeconds": 300},
        "generateAudio": {"timeoutSeconds": 300},
        "transcribeAudio": {"timeoutSeconds": 300},
        "analyzeVideo": {"timeoutSeconds": 1800},
    }
    assert [group["id"] for group in result["groups"]] == [
        "filesystem",
        "process",
        "web",
        "media",
        "sessions",
        "automations",
        "memory",
        "agent",
        "coding",
    ]
    assert {
        tool["source"]["type"]
        for group in result["groups"]
        for tool in group["tools"]
    } == {"builtin"}
    sensitive = next(
        action for group in result["groups"] for action in group["actions"]
        if action["capability"] == "sensitive_data.access"
    )
    assert (sensitive["policy"], sensitive["allowedPolicies"]) == (
        "auto", [policy.value for policy in ToolExecutionPolicy],
    )
    webfetch = next(
        tool
        for group in result["groups"]
        for tool in group["tools"]
        if tool["name"] == "webfetch"
    )
    assert {
        action["capability"]: action["available"]
        for action in webfetch["actions"]
    } == {"network.fetch": True}
    session_tools = next(group for group in result["groups"] if group["id"] == "sessions")
    discovery = {
        tool["name"]: tool["actions"][0]["capability"]
        for tool in session_tools["tools"]
        if tool["name"] in {"session_browse", "session_search"}
    }
    assert discovery == {
        "session_browse": "session.search",
        "session_search": "session.search",
    }


def test_tool_configuration_exposes_shared_web_access_settings() -> None:
    result = tool_configuration_payload(
        ToolSettings(
            web_access={
                "userAgent": "MatoLoop/1.0",
                "maxResponseMiB": 32,
                "publicAddressesOnly": False,
            }
        )
    )

    assert result["webAccess"] == {
        "userAgent": "MatoLoop/1.0",
        "maxResponseMiB": 32,
        "publicAddressesOnly": False,
    }


async def test_tool_availability_exposes_hard_caps(monkeypatch) -> None:
    settings = ToolSettings()
    monkeypatch.setattr(
        "mato_loop_backend.api.routes.tool_settings.load_effective_tools",
        AsyncMock(return_value=settings),
    )

    result = await get_tool_availability(
        SimpleNamespace(id=uuid4(), is_admin=False), _db()
    )

    assert result == {"tools": {}}


@pytest.mark.parametrize("timeout", [9, 3_601, 30.5])
def test_tool_settings_reject_invalid_media_timeout(timeout: float) -> None:
    with pytest.raises(ValidationError):
        MediaTimeoutRequest.model_validate({"timeoutSeconds": timeout})


def test_legacy_tool_policies_are_ignored_and_not_serialized() -> None:
    settings = ToolSettings.model_validate(
        {
            "generateImage": {"policy": "disabled", "timeoutSeconds": 600},
            "updateAgentIdentity": {"policy": "disabled"},
        }
    )

    data = settings.model_dump(mode="json")

    assert settings.generate_image.timeout_seconds == 600
    assert "policy" not in data["generate_image"]
    assert "update_agent_identity" not in data


def test_tool_settings_request_rejects_legacy_nested_policy() -> None:
    with pytest.raises(ValidationError):
        MediaTimeoutRequest.model_validate({"policy": "ask", "timeoutSeconds": 300})


@pytest.mark.parametrize("limit", [0, 257, 1.5])
def test_tool_settings_reject_invalid_web_access_limit(limit: float) -> None:
    with pytest.raises(ValidationError):
        WebAccessSettings.model_validate({"maxResponseMiB": limit})


@pytest.mark.parametrize(
    "user_agent",
    ["", "   ", "Mozilla/5.0\r\nX-Injected: true", "Mözilla/5.0", "x" * 513],
)
def test_tool_settings_reject_invalid_web_access_user_agent(user_agent: str) -> None:
    with pytest.raises(ValidationError):
        WebAccessSettings.model_validate({"userAgent": user_agent})
