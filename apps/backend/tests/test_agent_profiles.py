import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.permission_settings import _validate_permissions
from mato_loop_backend.api.routes import agent_profiles as agent_profiles_module
from mato_loop_backend.api.routes.agent_profiles import (
    AgentProfileRequest,
    _ensure_unique_name,
    _profile_from_request,
    _profile_payload,
    delete_agent_profile,
    list_agent_profiles,
    set_agent_profile_enabled,
    update_agent_profile,
)
from mato_loop_backend.integrations.tbs_desk.adapter import profile_id as tbs_profile_id
from mato_loop_backend.models import ChatSession, UserAccount
from mato_loop_backend.settings import (
    BUILD_PROFILE_ID,
    GENERAL_PROFILE_ID,
    PLAN_PROFILE_ID,
    SYSADMIN_PROFILE_ID,
    AgentIdentitySettings,
    AgentProfileOverrides,
    AgentProfilePermissions,
    AgentProfilesSettings,
    CustomAgentProfile,
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
    ToolExecutionPolicy,
    ToolSettings,
    UserAgentProfilesSettings,
    UserIntegrationSettings,
    core_profiles,
    load_agent_profile,
    load_effective_agent_profiles,
    resolve_agent_profile,
)


def test_builtin_agent_profile_defaults() -> None:
    profiles = core_profiles()
    default, plan, build, sysadmin = profiles

    assert [profile.id for profile in profiles] == [
        GENERAL_PROFILE_ID,
        PLAN_PROFILE_ID,
        BUILD_PROFILE_ID,
        SYSADMIN_PROFILE_ID,
    ]
    assert [profile.name for profile in profiles] == ["Allgemein", "Plan", "Build", "Sysadmin"]
    assert [profile.repository_instructions_enabled for profile in profiles] == [False, True, True, False]
    assert [profile.temporary_files_enabled for profile in profiles] == [False, True, True, True]
    assert AgentProfilesSettings().profiles == []
    assert "persona_id" not in type(default).model_fields
    assert default.private_permissions == AgentProfilePermissions()
    assert default.public_permissions == AgentProfilePermissions()
    assert plan.private_permissions == plan.public_permissions
    assert plan.private_permissions.policies == {
        "filesystem.read": ToolExecutionPolicy.AUTO,
        "filesystem.write": ToolExecutionPolicy.ASK,
        "filesystem.delete": ToolExecutionPolicy.ASK,
        "working_directory.external": ToolExecutionPolicy.ASK,
        "session.files.write": ToolExecutionPolicy.ASK,
        "session.files.delete": ToolExecutionPolicy.ASK,
        "shell.read": ToolExecutionPolicy.AUTO,
        "shell.write": ToolExecutionPolicy.ASK,
        "shell.delete": ToolExecutionPolicy.ASK,
    }
    assert build.private_permissions == build.public_permissions
    assert build.private_permissions.policies == {
        "filesystem.read": ToolExecutionPolicy.AUTO,
        "filesystem.write": ToolExecutionPolicy.AUTO,
        "filesystem.delete": ToolExecutionPolicy.AUTO,
        "working_directory.external": ToolExecutionPolicy.ASK,
        "shell.read": ToolExecutionPolicy.AUTO,
        "shell.write": ToolExecutionPolicy.AUTO,
        "shell.delete": ToolExecutionPolicy.AUTO,
    }
    assert sysadmin.private_permissions == sysadmin.public_permissions
    assert sysadmin.private_permissions.policies == {
        "filesystem.read": ToolExecutionPolicy.AUTO,
        "filesystem.write": ToolExecutionPolicy.ASK,
        "filesystem.delete": ToolExecutionPolicy.ASK,
        "working_directory.external": ToolExecutionPolicy.AUTO,
        "shell.read": ToolExecutionPolicy.AUTO,
        "shell.write": ToolExecutionPolicy.ASK,
        "shell.delete": ToolExecutionPolicy.ASK,
    }
    assert not plan.private_permissions.integration_policies
    assert not build.private_permissions.integration_policies
    for profile in (plan, build):
        assert "simplest" in profile.instructions
        assert "current requirements justify" in profile.instructions
        assert "standard-library or native features" in profile.instructions
    assert default.delegation_enabled is False
    assert default.session_write_paths is None
    assert plan.delegation_enabled is True
    assert plan.session_write_paths is None
    assert build.delegation_enabled is True
    assert build.session_write_paths is None
    assert sysadmin.max_tool_calls is None
    assert sysadmin.todo_enabled is True
    assert sysadmin.integrations_enabled_by_default is False
    assert sysadmin.session_write_paths is None
    assert "host approval flow" in sysadmin.instructions
    assert "plan directly in the conversation" in plan.instructions
    assert "Do not implement" in plan.instructions
    assert "shortest actionable plan" in plan.instructions
    assert "not an implementation plan" in plan.instructions
    assert "do not substitute a simpler but different feature" in plan.instructions
    assert "Do not restate the request or add boilerplate sections" in plan.instructions
    assert "Resolve missing implementation facts from the code rather than asking the user" in plan.instructions
    assert "Do not turn such choices into assumptions" in plan.instructions
    assert "do not call any tool" not in plan.instructions
    assert "continue only permitted analysis and planning" in plan.runtime_instructions
    assert default.runtime_instructions == build.runtime_instructions == ""
    assert "keep an analysis-only task read-only" in build.instructions
    assert "plan discussed earlier in the conversation" in build.instructions
    assert "profile switch alone does not authorize implementation" in build.instructions
    assert "plan.md" not in plan.instructions
    assert "plan.md" not in build.instructions


async def test_disabled_personal_agent_profiles_remain_readable(monkeypatch) -> None:
    user = UserAccount(id=uuid4(), username="user", password_hash="hash")
    personal = UserAgentProfilesSettings()
    db = AsyncMock()
    load = AsyncMock(return_value=personal)
    response = AsyncMock(
        return_value={"personalAgentsEnabled": False, "agents": []}
    )
    scoped = AsyncMock(side_effect=AssertionError("read must not use the write gate"))
    monkeypatch.setattr(SettingsStore, "load", load)
    monkeypatch.setattr(agent_profiles_module, "_response", response)
    monkeypatch.setattr(agent_profiles_module, "_load_scoped", scoped)

    result = await list_agent_profiles(user, db, scope="personal")

    assert result == {"personalAgentsEnabled": False, "agents": []}
    load.assert_awaited_once_with(db, UserAgentProfilesSettings, user_id=user.id)
    response.assert_awaited_once_with(db, user, personal, "personal")
    scoped.assert_not_awaited()


async def test_disabled_personal_profiles_are_manageable_but_not_runtime_effective(monkeypatch) -> None:
    user = UserAccount(id=uuid4(), username="user", password_hash="hash")
    custom = CustomAgentProfile(
        name="Mine", description="", base_profile_id=GENERAL_PROFILE_ID,
        overrides=AgentProfileOverrides(),
    )
    instance = AgentProfilesSettings(personal_agents_enabled=False)

    async def load(_db, settings_type, **_kwargs):
        if settings_type is AgentProfilesSettings:
            return instance
        if settings_type is AgentIdentitySettings:
            return AgentIdentitySettings()
        if settings_type is IntegrationSettings:
            return IntegrationSettings()
        return ToolSettings()

    async def load_optional(_db, settings_type, **_kwargs):
        if settings_type is AgentProfilesSettings:
            return instance
        if settings_type is UserAgentProfilesSettings:
            return UserAgentProfilesSettings(profiles=[custom])
        return None

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(side_effect=load_optional))
    db = AsyncMock()

    management = await list_agent_profiles(user, db, scope="personal")
    runtime = await list_agent_profiles(user, db)

    assert [(item["name"], item["effective"]) for item in management["agents"]] == [("Mine", False)]
    assert all(item["id"] != str(custom.id) for item in runtime["effectiveAgents"])


async def test_custom_profile_switch_is_owner_scoped_and_shipped_profiles_stay_enabled(monkeypatch) -> None:
    user = UserAccount(id=uuid4(), username="user", password_hash="hash")
    custom = CustomAgentProfile(
        name="Mine", description="", base_profile_id=GENERAL_PROFILE_ID,
        overrides=AgentProfileOverrides(),
    )
    settings = UserAgentProfilesSettings(profiles=[custom])
    load_scoped = AsyncMock(return_value=(settings, user.id))
    save = AsyncMock()
    monkeypatch.setattr(agent_profiles_module, "_load_scoped", load_scoped)
    monkeypatch.setattr(agent_profiles_module, "_response", AsyncMock(return_value={}))
    monkeypatch.setattr(SettingsStore, "save", save)
    db = AsyncMock()

    await set_agent_profile_enabled(
        custom.id, agent_profiles_module.EnabledRequest(enabled=False), user, db,
        scope="personal",
    )

    assert custom.enabled is False
    save.assert_awaited_once_with(db, settings, user_id=user.id)
    db.commit.assert_awaited_once()
    with pytest.raises(ApiError, match="agentProfile.defaultProtected"):
        await set_agent_profile_enabled(
            GENERAL_PROFILE_ID, agent_profiles_module.EnabledRequest(enabled=False), user, db,
            scope="instance",
        )
    assert load_scoped.await_count == 2


async def test_disabled_profile_stays_resolvable_for_an_active_run(monkeypatch) -> None:
    user_id = uuid4()
    custom = CustomAgentProfile(
        name="Paused", description="", base_profile_id=GENERAL_PROFILE_ID,
        overrides=AgentProfileOverrides(), enabled=False,
    )

    async def load_optional(_db, kind, **kwargs):
        if kind is AgentProfilesSettings:
            return AgentProfilesSettings()
        if kind is UserAgentProfilesSettings:
            assert kwargs["user_id"] == user_id
            return UserAgentProfilesSettings(profiles=[custom])
        return None

    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(side_effect=load_optional))

    profile = await load_agent_profile(AsyncMock(), user_id, custom.id)

    assert profile.id == custom.id
    assert profile.enabled is False


def test_custom_profile_stores_only_sparse_typed_overrides() -> None:
    profile = CustomAgentProfile(
        name="Custom",
        description="Metadata",
        baseProfileId=PLAN_PROFILE_ID,
        overrides={
            "todoEnabled": False,
            "repositoryInstructionsEnabled": False,
            "maxToolCalls": None,
            "privatePermissions": {},
            "sessionWritePaths": [],
        },
    )
    saved = AgentProfilesSettings(profiles=[profile]).model_dump(mode="json", by_alias=True)

    assert saved["profiles"][0]["overrides"] == {
        "todoEnabled": False,
        "repositoryInstructionsEnabled": False,
        "maxToolCalls": None,
        "privatePermissions": {},
        "sessionWritePaths": [],
    }
    assert AgentProfilesSettings.model_validate(saved).model_dump(mode="json", by_alias=True) == saved
    effective = resolve_agent_profile(profile, core_profiles()[1], None)
    assert effective.todo_enabled is False
    assert effective.repository_instructions_enabled is False
    assert effective.session_write_paths == []
    assert effective.public_permissions == core_profiles()[1].public_permissions


def test_runtime_instructions_inherit_override_and_clear() -> None:
    base = core_profiles()[1]
    profile = CustomAgentProfile(name="Custom", description="", baseProfileId=base.id, overrides={})
    assert resolve_agent_profile(profile, base, None).runtime_instructions == base.runtime_instructions
    for text in ("Inspect only; do not publish.", ""):
        profile.overrides = AgentProfileOverrides(runtimeInstructions=text)
        saved = CustomAgentProfile.model_validate(profile.model_dump(mode="json", by_alias=True))
        assert resolve_agent_profile(saved, base, None).runtime_instructions == text
    with pytest.raises(ValidationError):
        AgentProfileOverrides(runtimeInstructions=None)


@pytest.mark.parametrize(
    "path", ["", "/plan.md", "./plan.md", "notes//plan.md", "notes/../plan.md",
             "notes/", "*.md", "notes\\plan.md"]
)
def test_session_write_paths_require_normalized_relative_files(path: str) -> None:
    with pytest.raises(ValidationError):
        AgentProfileOverrides(sessionWritePaths=[path])


def test_core_profiles_preserve_instance_custom_profile_capacity() -> None:
    custom = [
        CustomAgentProfile(
            name=f"Custom {index}", description="", baseProfileId=GENERAL_PROFILE_ID,
            overrides={},
        )
        for index in range(99)
    ]

    AgentProfilesSettings(profiles=custom)
    with pytest.raises(ValidationError):
        AgentProfilesSettings(
            profiles=[
                *custom,
                CustomAgentProfile(
                    name="One too many", description="", baseProfileId=GENERAL_PROFILE_ID,
                    overrides={},
                ),
            ]
        )


@pytest.mark.parametrize("name", ["allgemein", "pLaN", "BUILD"])
def test_builtin_agent_profile_names_are_reserved_case_insensitively(name: str) -> None:
    with pytest.raises(ApiError, match="agentProfile.defaultProtected"):
        _ensure_unique_name(AgentProfilesSettings(), name)


def test_default_is_not_a_reserved_profile_name() -> None:
    _ensure_unique_name(AgentProfilesSettings(), "Default")


def test_agent_profile_payload_marks_only_builtins_as_protected() -> None:
    identity = AgentIdentitySettings()

    builtin_payload = _profile_payload(
        core_profiles()[1], "instance", identity, None
    )
    assert builtin_payload["protected"] is True
    assert builtin_payload["baseProfileId"] is None
    assert "overrides" not in builtin_payload
    assert builtin_payload["delegationEnabled"] is True
    assert builtin_payload["sessionWritePaths"] is None
    custom = CustomAgentProfile(
        name="Custom", description="", baseProfileId=GENERAL_PROFILE_ID,
        overrides={"todoEnabled": False, "sessionWritePaths": []},
    )
    custom_payload = _profile_payload(
        resolve_agent_profile(custom, core_profiles()[0], None),
        "instance", identity, None, custom,
    )
    assert custom_payload["protected"] is False
    assert custom_payload["baseProfileId"] == str(GENERAL_PROFILE_ID)
    assert custom_payload["overrides"] == {
        "todoEnabled": False,
        "sessionWritePaths": [],
    }
    assert custom_payload["instructions"] == core_profiles()[0].instructions


@pytest.mark.parametrize("profile_id", [PLAN_PROFILE_ID, BUILD_PROFILE_ID])
async def test_plan_and_build_cannot_be_renamed_or_deleted(
    monkeypatch, profile_id
) -> None:
    settings = AgentProfilesSettings()
    user = UserAccount(
        id=uuid4(), username="admin", password_hash="hash", is_admin=True
    )
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))

    with pytest.raises(ApiError, match="agentProfile.defaultProtected"):
        await update_agent_profile(
            profile_id,
            AgentProfileRequest(
                name="Renamed", description="", baseProfileId=profile_id, overrides={}
            ),
            user,
            AsyncMock(),
            scope="instance",
        )
    with pytest.raises(ApiError, match="agentProfile.defaultProtected"):
        await delete_agent_profile(
            profile_id,
            user,
            AsyncMock(),
            scope="instance",
        )

    user.is_admin = False
    with pytest.raises(ApiError, match="auth.adminRequired"):
        await delete_agent_profile(profile_id, user, AsyncMock(), scope="instance")


async def test_delete_agent_profile_moves_sessions_to_default_and_clears_grants(
    monkeypatch,
) -> None:
    profile = CustomAgentProfile(
        name="Custom", description="", baseProfileId=GENERAL_PROFILE_ID,
        overrides={},
    )
    settings = AgentProfilesSettings(profiles=[profile])
    user = UserAccount(
        id=uuid4(), username="admin", password_hash="hash", is_admin=True
    )
    sessions = [
        ChatSession(id=uuid4(), user_id=user.id, agent_profile_id=profile.id),
        ChatSession(id=uuid4(), user_id=uuid4(), agent_profile_id=profile.id),
    ]
    rows = Mock()
    rows.all.return_value = sessions
    db = AsyncMock()
    db.scalars.return_value = rows
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())

    await delete_agent_profile(profile.id, user, db, scope="instance")

    assert all(
        session.agent_profile_id == GENERAL_PROFILE_ID for session in sessions
    )
    assert profile not in settings.profiles
    db.execute.assert_awaited_once()
    assert "DELETE FROM permission_grants" in str(db.execute.await_args.args[0])
    SettingsStore.save.assert_awaited_once_with(db, settings, user_id=None)
    db.commit.assert_awaited_once()


def test_agent_profile_migration_installs_inheriting_default_profile() -> None:
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/6bf7ebf0c5b3_add_agent_profiles_to_sessions.py"
    )
    spec = importlib.util.spec_from_file_location("agent_profile_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.DEFAULT_AGENT_PROFILE_ID == GENERAL_PROFILE_ID
    profile = migration._default_profile()
    assert profile["persona"] == ""
    assert profile["private_permissions"]["policies"] == {}
    assert profile["public_permissions"]["policies"] == {}


def test_profile_inheritance_migration_resets_only_current_profile_state(monkeypatch) -> None:
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/8cf1ced587b7_reset_agent_profiles_for_inheritance.py"
    )
    spec = importlib.util.spec_from_file_location("profile_inheritance_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    execute = Mock()
    rows = Mock()
    rows.scalars.return_value = []
    connection = Mock()
    connection.execute.return_value = rows
    monkeypatch.setattr(migration.op, "execute", execute)
    monkeypatch.setattr(migration.op, "get_bind", Mock(return_value=connection))

    migration.upgrade()

    statements = "\n".join(str(call.args[0]) for call in execute.call_args_list)
    assert "UPDATE settings" in statements
    assert "DELETE FROM permission_grants" in statements
    assert "UPDATE chat_sessions" in statements
    assert "UPDATE agent_runs" in statements
    assert "status NOT IN ('completed', 'failed', 'cancelled')" in statements
    assert "messages" not in statements

    execute.reset_mock()
    migration.downgrade()
    downgrade_statements = [call.args[0] for call in execute.call_args_list]
    statements = "\n".join(str(statement) for statement in downgrade_statements)
    downgrade = next(
        statement for statement in downgrade_statements
        if "CASE WHEN user_id IS NULL" in str(statement)
    )
    assert "WHERE key = 'agent-profiles'" in str(downgrade)
    profiles = downgrade.compile().params["profiles"]
    assert [profile["name"] for profile in profiles] == ["Allgemein", "Plan", "Build"]
    assert profiles[1]["private_permissions"]["policies"]["filesystem.write"] == "disabled"
    assert profiles[2]["private_permissions"]["policies"]["filesystem.write"] == "auto"
    assert "DELETE FROM permission_grants" in statements
    assert "UPDATE chat_sessions" in statements
    assert "UPDATE agent_runs" in statements


def test_policy_migration_splits_trust_branches_and_webfetch_targets() -> None:
    migration = _migration_module()
    migrated = migration._migrate_tool_data(
        {
            "policies": {"filesystem.read": "auto"},
            "integrationPolicies": {"one": {"calendar.read": "ask"}},
            "resourcePolicies": {"one": {"calendar:work": {"calendar.read": "auto"}}},
            "webFetch": {
                "enabled": True,
                "defaultPolicy": "auto_allowed",
                "allowedPrivateOrigins": ["http://internal.example"],
            },
        },
        ["disabled"],
    )

    assert migrated["private_permissions"] == migrated["public_permissions"]
    assert migrated["public_permissions"]["policies"]["network.fetch.public"] == "disabled"
    assert migrated["public_permissions"]["policies"]["network.fetch.private"] == "disabled"
    assert migrated["resource_availability"]["webfetch"] == {
        "network.fetch.public": True,
        "network.fetch.private": False,
    }
    assert "allowedPrivateOrigins" not in migrated["web_fetch"]


def test_policy_migration_creates_tools_for_explicit_model_policy(monkeypatch) -> None:
    migration = _migration_module()
    user_id = uuid4()
    existing_user_id = uuid4()
    rows = Mock()
    rows.mappings.return_value.all.return_value = [
        {
            "id": uuid4(),
            "key": "ai",
            "user_id": user_id,
            "data": {"models": [{"webfetchPolicy": "disabled"}]},
        },
        {
            "id": uuid4(),
            "key": "ai",
            "user_id": existing_user_id,
            "data": {"models": [{"webfetchPolicy": "ask"}]},
        },
        {
            "id": uuid4(),
            "key": "tools",
            "user_id": existing_user_id,
            "data": {"personal_tool_defaults_enabled": False},
        },
    ]
    connection = Mock()
    connection.execute.side_effect = [rows, None, None, None, None]
    monkeypatch.setattr(migration.op, "get_bind", Mock(return_value=connection))
    monkeypatch.setattr(migration.op, "add_column", Mock())
    monkeypatch.setattr(migration.op, "create_check_constraint", Mock())
    monkeypatch.setattr(migration.op, "execute", Mock())
    monkeypatch.setattr(migration.op, "alter_column", Mock())
    monkeypatch.setattr(migration, "_install_default_profile", Mock())

    migration.upgrade()

    insert_call = next(
        call for call in connection.execute.call_args_list if call.args[0].is_insert
    )
    document = insert_call.args[1][0]
    assert document["user_id"] == user_id
    assert document["data"]["personal_tool_defaults_enabled"] is True
    assert document["data"]["public_permissions"]["policies"] == {
        "network.fetch.public": "disabled",
        "network.fetch.private": "disabled",
    }
    existing_document = connection.execute.call_args_list[3].args[0].compile().params[
        "data"
    ]
    assert existing_document["personal_tool_defaults_enabled"] is True


def test_policy_downgrade_preserves_webfetch_hard_disable() -> None:
    migration = _migration_module()

    downgraded = migration._downgrade_tool_data(
        {
            "public_permissions": {
                "policies": {
                    "network.fetch.public": "auto",
                    "network.fetch.private": "ask",
                }
            },
            "private_permissions": {},
            "tool_availability": {"webfetch": False},
            "resource_availability": {
                "webfetch": {
                    "network.fetch.public": True,
                    "network.fetch.private": False,
                }
            },
        }
    )

    assert downgraded["web_fetch"]["enabled"] is False
    assert downgraded["web_fetch"]["default_policy"] == "disabled"


def _migration_module():
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/6bf7ebf0c5b3_add_agent_profiles_to_sessions.py"
    )
    spec = importlib.util.spec_from_file_location("agent_profile_policy_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def test_effective_agent_profiles_merge_enabled_personal_profiles(
    monkeypatch,
) -> None:
    user_id = uuid4()
    instance = AgentProfilesSettings(
        profiles=[CustomAgentProfile(
            name="Global", description="Instance", baseProfileId=GENERAL_PROFILE_ID,
            overrides={"instructions": "Global task"},
        )]
    )
    personal = UserAgentProfilesSettings(
        profiles=[CustomAgentProfile(
            name="Personal", description="Personal", baseProfileId=PLAN_PROFILE_ID,
            overrides={"instructions": "Personal task", "todoEnabled": False},
        )]
    )
    async def load(_db, settings_type, **_kwargs):
        return IntegrationSettings()

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    monkeypatch.setattr(
        SettingsStore, "load_optional", AsyncMock(
            side_effect=lambda _db, settings_type, **_kwargs: (
                instance if settings_type is AgentProfilesSettings else
                personal if settings_type is UserAgentProfilesSettings else None
            )
        )
    )

    effective = await load_effective_agent_profiles(AsyncMock(), user_id)

    assert [profile.name for profile in effective.profiles] == [
        "Allgemein", "Plan", "Build", "Sysadmin", "Global", "Personal"
    ]
    assert effective.profiles[4].owner_id is None
    assert effective.profiles[5].owner_id == user_id
    assert effective.profiles[5].delegation_enabled is True
    assert effective.profiles[5].todo_enabled is False


async def test_builtin_access_inherits_per_entry_and_respects_scope(monkeypatch) -> None:
    from mato_loop_backend.settings.models import AgentProfileAccessOverrides

    integration_id = str(uuid4())
    child = CustomAgentProfile(name="Child", description="", baseProfileId=PLAN_PROFILE_ID,
        overrides={"privatePermissions": {"policies": {"shell.read": "ask"}}})
    instance = AgentProfilesSettings(profiles=[child], builtin_access_overrides={PLAN_PROFILE_ID:
        AgentProfileAccessOverrides(integrationsEnabledByDefault=True,
            privatePermissions={"policies": {"filesystem.read": "ask"},
                "integrationPolicies": {integration_id: {"browser.use": "auto", "browser.code": "ask"}},
                "resourcePolicies": {integration_id: {"page": {"browser.use": "ask"}}}})})
    personal = UserAgentProfilesSettings(profiles=[CustomAgentProfile(
        name="Personal", description="", baseProfileId=PLAN_PROFILE_ID,
        overrides={"integrationsEnabledByDefault": False, "privatePermissions": {
            "policies": {"shell.read": "ask"},
            "integrationPolicies": {integration_id: {"browser.use": "disabled"}}}},
    )])
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=IntegrationSettings()))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(side_effect=lambda db, kind, **kw:
        instance if kind is AgentProfilesSettings else
        personal if kind is UserAgentProfilesSettings else None))

    async def get(include_personal=True):
        return (await load_effective_agent_profiles(AsyncMock(), uuid4(), include_personal=include_personal)).profiles[-1]

    effective = await get()
    assert effective.integrations_enabled_by_default is False
    assert effective.private_permissions.policies["filesystem.write"] == ToolExecutionPolicy.ASK
    assert effective.private_permissions.policies["filesystem.read"] == ToolExecutionPolicy.ASK
    assert effective.private_permissions.policies["shell.read"] == ToolExecutionPolicy.ASK
    assert effective.private_permissions.integration_policies[integration_id] == {
        "browser.use": ToolExecutionPolicy.DISABLED, "browser.code": ToolExecutionPolicy.ASK}
    assert effective.private_permissions.resource_policies[integration_id]["page"]["browser.use"] == ToolExecutionPolicy.ASK
    assert (await get(False)).integrations_enabled_by_default is True
    instance.builtin_access_overrides[PLAN_PROFILE_ID].private_permissions.policies["filesystem.read"] = ToolExecutionPolicy.AUTO
    assert (await get()).private_permissions.policies["filesystem.read"] == ToolExecutionPolicy.AUTO


def test_builtin_access_documents_are_sparse_and_restrict_fields() -> None:
    from mato_loop_backend.settings.models import AgentProfileAccessOverrides

    original = AgentProfilesSettings(builtin_access_overrides={PLAN_PROFILE_ID:
        AgentProfileAccessOverrides(integrationsEnabledByDefault=False)})
    restored = AgentProfilesSettings.model_validate(original.model_dump(mode="json", by_alias=True))
    override = restored.builtin_access_overrides[PLAN_PROFILE_ID]
    assert override.model_fields_set == {"integrations_enabled_by_default"}
    assert override.integrations_enabled_by_default is False
    assert "builtin_access_overrides" not in UserAgentProfilesSettings.model_fields
    with pytest.raises(ValidationError):
        AgentProfilesSettings(builtin_access_overrides={uuid4(): {}})
    for field in ("instructions", "runtimeInstructions", "personaId", "delegationEnabled"):
        with pytest.raises(ValidationError):
            AgentProfileAccessOverrides.model_validate({field: "changed"})
    with pytest.raises(ValidationError):
        AgentProfileAccessOverrides(publicPermissions=None)


async def test_builtin_access_updates_require_administration_scope(monkeypatch) -> None:
    from mato_loop_backend.api.routes.tool_settings import (
        ProfileToolPermissionsRequest,
        update_profile_tool_permissions,
    )

    settings = AgentProfilesSettings()
    user = UserAccount(id=uuid4(), username="admin", password_hash="hash", is_admin=True)
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda db, kind, **kw:
        settings if kind is AgentProfilesSettings else ToolSettings(personal_settings_enabled=True) if kind is ToolSettings else kind()))
    save = AsyncMock()
    monkeypatch.setattr(SettingsStore, "save", save)
    monkeypatch.setattr("mato_loop_backend.api.routes.agent_profiles._response", AsyncMock(return_value={}))
    db = AsyncMock()
    await update_profile_tool_permissions(PLAN_PROFILE_ID, ProfileToolPermissionsRequest(
        privatePermissions={"policies": {"filesystem.read": "ask"}}), user, db, scope="instance")
    assert settings.builtin_access_overrides[PLAN_PROFILE_ID].private_permissions.policies["filesystem.read"] == ToolExecutionPolicy.ASK
    user.is_admin = False
    with pytest.raises(ApiError, match="auth.adminRequired"):
        await update_profile_tool_permissions(PLAN_PROFILE_ID, ProfileToolPermissionsRequest(), user, db, scope="instance")
    settings.personal_agents_enabled = True
    with pytest.raises(ApiError, match="agentProfile.notFound"):
        await update_profile_tool_permissions(PLAN_PROFILE_ID, ProfileToolPermissionsRequest(), user, db, scope="personal")
    settings.personal_agents_enabled = False
    with pytest.raises(ApiError, match="agentProfile.personalDisabled"):
        await update_profile_tool_permissions(PLAN_PROFILE_ID, ProfileToolPermissionsRequest(), user, db, scope="personal")
    save.assert_awaited_once()


async def test_integration_profiles_remain_available_when_personal_profiles_are_disabled(
    monkeypatch,
) -> None:
    user_id = uuid4()
    integration = IntegrationInstance(
        name="Support",
        integration_type="support",
        adapter_type="tbs-desk",
        config={"base_url": "https://desk.example", "username": "agent"},
    )

    async def load(_db, settings_type, **_kwargs):
        return IntegrationSettings()

    async def load_optional(_db, settings_type, **_kwargs):
        if settings_type is AgentProfilesSettings:
            return AgentProfilesSettings(personal_agents_enabled=False)
        if settings_type is UserIntegrationSettings:
            return UserIntegrationSettings(instances=[integration])
        return None

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(side_effect=load_optional))

    effective = await load_effective_agent_profiles(AsyncMock(), user_id)

    profile = next(item for item in effective.profiles if item.id == tbs_profile_id(integration.id))
    assert profile.owner_id == user_id
    assert profile.integration_id == integration.id
    assert profile.editable is False


def test_tool_permissions_reject_unknown_or_disallowed_capabilities() -> None:
    allowed = {"filesystem.read": {ToolExecutionPolicy.AUTO}}

    _validate_permissions(
        AgentProfilePermissions(policies={"filesystem.read": "auto"}),
        allowed,
        "privatePermissions",
    )

    with pytest.raises(ApiError):
        _validate_permissions(
            AgentProfilePermissions(policies={"filesystem.read": "ask"}),
            allowed,
            "publicPermissions",
        )
    with pytest.raises(ApiError):
        _validate_permissions(
            AgentProfilePermissions(policies={"unknown.action": "auto"}),
            allowed,
            "publicPermissions",
        )


@pytest.mark.parametrize("field", ["integration_policies", "resource_policies"])
def test_tool_permissions_reject_scoped_overrides(field) -> None:
    policies = {"mail.read": "ask"} if field == "integration_policies" else {"account": {"mail.read": "ask"}}
    with pytest.raises(ApiError, match="validation.invalidChoice"):
        _validate_permissions(
            AgentProfilePermissions.model_validate({field: {str(uuid4()): policies}}),
            {"mail.read": {ToolExecutionPolicy.ASK}},
            "privatePermissions",
        )


def test_agent_profile_request_rejects_blank_trimmed_text() -> None:
    with pytest.raises(ValidationError):
        AgentProfileRequest(
            name="  ", description="", baseProfileId=GENERAL_PROFILE_ID, overrides={}
        )
    with pytest.raises(ValidationError):
        AgentProfileRequest(
            name="Agent", description="", baseProfileId=GENERAL_PROFILE_ID,
            overrides={"instructions": "  "},
        )

    with pytest.raises(ValidationError):
        AgentProfileRequest(name="Agent", description="", baseProfileId=GENERAL_PROFILE_ID,
                            overrides={"personaId": uuid4()})


@pytest.mark.parametrize("overrides", [{"temporaryFilesEnabled": False}, {}])
async def test_agent_profile_request_replaces_sparse_overrides(monkeypatch, overrides) -> None:
    async def load_settings(_db, settings_type, **_kwargs):
        return settings_type()

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load_settings))
    existing = CustomAgentProfile(
        name="Agent", description="Old", baseProfileId=GENERAL_PROFILE_ID,
        enabled=False,
        overrides={"instructions": "Old task", "todoEnabled": True, "temporaryFilesEnabled": False},
    )
    user = UserAccount(
        id=uuid4(), username="admin", password_hash="hash", is_admin=True
    )
    payload = AgentProfileRequest(
        name="Agent", description="New", baseProfileId=PLAN_PROFILE_ID,
        overrides={"instructions": "New task", **overrides},
    )

    updated = await _profile_from_request(
        AsyncMock(),
        user,
        None,
        payload,
        profile_id=existing.id,
        existing=existing,
    )

    assert updated.base_profile_id == PLAN_PROFILE_ID
    assert updated.description == "New"
    assert updated.enabled is False
    assert updated.overrides.model_dump(exclude_unset=True, by_alias=True) == {"instructions": "New task", **overrides}
