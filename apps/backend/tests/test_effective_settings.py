from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.integration_access import _load_scoped_integrations
from mato_loop_backend.settings import (
    AiProviderSettings,
    AiSettings,
    GeneralSettings,
    IntegrationAdapterAvailability,
    IntegrationInstance,
    IntegrationSettings,
    SettingsScope,
    SettingsStore,
    ToolSettings,
    UserAiSettings,
    UserIntegrationSettings,
    UserToolSettings,
    WebAccessSettings,
    load_effective_ai,
    load_effective_integrations,
    load_effective_tools,
)
from mato_loop_backend.settings.effective import personal_integration_tool_settings
from mato_loop_backend.settings.models import (
    LlmProviderConfig,
    ModelConfig,
    ModelDefaults,
    ModelDefaultsSettings,
    effective_model_trust,
)


def test_personal_settings_models_and_instance_flags_have_safe_defaults() -> None:
    assert AiSettings().personal_providers_enabled is False
    assert ToolSettings().personal_settings_enabled is False
    assert AiSettings().title_vision_fallback_enabled is True
    assert "title_vision_fallback_enabled" not in UserAiSettings.model_fields
    integrations = IntegrationSettings(adapter_availability={"caldav": "full"})
    assert integrations.adapter_availability == {}
    assert (
        integrations.availability_for("new-adapter")
        == IntegrationAdapterAvailability.FULL
    )
    assert UserAiSettings.settings_scope == SettingsScope.USER
    assert UserIntegrationSettings.settings_scope == SettingsScope.USER
    assert UserToolSettings.settings_scope == SettingsScope.USER


def test_provider_settings_do_not_persist_obsolete_defaults() -> None:
    for settings_type in (AiProviderSettings, UserAiSettings):
        settings = settings_type.model_validate({"enabled": False, "default_model_id": str(uuid4()), "reasoning_effort": "high"})

        assert not ({"enabled"} | set(ModelDefaults.model_fields)) & settings.model_dump().keys()


def test_integration_settings_normalizes_legacy_availability() -> None:
    disabled = IntegrationSettings.model_validate(
        {
            "personal_integrations_enabled": False,
            "allowed_personal_adapter_types": ["caldav", "openscad"],
            "disabled_integration_adapter_types": ["caldav"],
        }
    )
    partially_allowed = IntegrationSettings.model_validate(
        {"allowed_personal_adapter_types": ["caldav", "openscad"]}
    )

    assert disabled.availability_for("caldav") == IntegrationAdapterAvailability.DISABLED
    assert (
        disabled.availability_for("searxng")
        == IntegrationAdapterAvailability.INSTANCE_ONLY
    )
    assert disabled.availability_for("openscad") == IntegrationAdapterAvailability.DISABLED
    assert (
        partially_allowed.availability_for("searxng")
        == IntegrationAdapterAvailability.INSTANCE_ONLY
    )
    assert (
        partially_allowed.availability_for("openscad")
        == IntegrationAdapterAvailability.FULL
    )
    assert set(disabled.model_dump()) == {"instances", "adapter_availability", "webfetch_integration_id"}


@pytest.mark.parametrize("model_trust_enabled", [True, False])
async def test_effective_ai_combines_provider_inventory_without_personal_defaults(
    monkeypatch, model_trust_enabled,
) -> None:
    user_id = uuid4()
    instance_provider = LlmProviderConfig(name="Instance", adapter_type="openai-compatible")
    instance_model = ModelConfig(provider_id=instance_provider.id, model_id="instance")
    personal_provider = LlmProviderConfig(name="Personal", adapter_type="openai-compatible")
    personal_model = ModelConfig(provider_id=personal_provider.id, model_id="personal")
    unavailable_model = ModelConfig(
        provider_id=personal_provider.id, model_id="disabled", enabled=False
    )
    instance = AiSettings(
        personal_providers_enabled=True,
        providers=[instance_provider],
        models=[instance_model],
        default_model_id=instance_model.id,
        compaction_model_id=instance_model.id,
        title_generation_model_id=instance_model.id,
        title_vision_fallback_enabled=False,
    )
    personal = UserAiSettings.model_validate(
        {
            "enabled": False,
            "providers": [personal_provider],
            "models": [personal_model, unavailable_model],
            "default_model_id": personal_model.id,
            "compaction_model_id": unavailable_model.id,
            "title_vision_fallback_enabled": True,
        }
    )
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=instance))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(side_effect=lambda _db, model, **_kw: personal if model is UserAiSettings else ModelDefaultsSettings(**instance.model_dump(include=set(ModelDefaults.model_fields))) if model is ModelDefaultsSettings else GeneralSettings(modelTrustEnabled=model_trust_enabled) if model is GeneralSettings else None))

    effective = await load_effective_ai(AsyncMock(spec=AsyncSession), user_id)

    assert [provider.owner_id for provider in effective.providers] == [None, user_id]
    assert [model.id for model in effective.models] == [
        instance_model.id,
        personal_model.id,
        unavailable_model.id,
    ]
    assert effective.default_model_id == instance_model.id
    assert effective.compaction_model_id == instance_model.id
    assert effective.title_generation_model_id == instance_model.id
    assert effective.title_vision_fallback_enabled is False
    assert (
        [effective_model_trust(provider, model).value for provider in effective.providers for model in effective.models if model.provider_id == provider.id],
        instance_provider.trust_level.value, personal_provider.trust_level.value,
    ) == (["public" if model_trust_enabled else "private"] * 3, "public", "public")
    assert instance_provider.owner_id is None
    assert personal_provider.owner_id is None
    assert "owner_id" not in effective.providers[1].model_dump()


async def test_effective_integrations_adds_only_allowed_personal_instances(monkeypatch) -> None:
    user_id = uuid4()
    agent_id = uuid4()
    global_item = IntegrationInstance(
        name="Global OpenSCAD",
        integration_type="openscad",
        adapter_type="openscad",
        execution_agent_id=agent_id,
        config={"binary_path": "/usr/bin/openscad"},
    )
    allowed = IntegrationInstance(
        name="Personal Calendar",
        integration_type="calendar",
        adapter_type="caldav",
        config={"base_url": "https://calendar.example.com", "username": "user"},
    )
    blocked = IntegrationInstance(
        name="Personal Knowledge",
        integration_type="knowledge",
        adapter_type="bookstack",
    )
    instance = IntegrationSettings(
        instances=[global_item], adapter_availability={"bookstack": "instance_only"}
    )
    personal = UserIntegrationSettings(instances=[allowed, blocked, IntegrationInstance(
        name="Personal Reader", integration_type="webfetch", adapter_type="jina",
        config={"base_url": "https://reader.test"},
    )])
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=instance))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(return_value=personal))
    db = AsyncMock(spec=AsyncSession)
    agents = type("Rows", (), {"all": lambda _self: [agent_id]})()
    db.scalars.return_value = agents

    effective = await load_effective_integrations(db, user_id)

    assert [item.id for item in effective.instances] == [global_item.id, allowed.id]
    assert [item.owner_id for item in effective.instances] == [None, user_id]
    assert "execution_agents.user_id" in str(db.scalars.await_args.args[0])


async def test_effective_integrations_excludes_unowned_agent_bindings(monkeypatch) -> None:
    user_id = uuid4()
    global_agent = IntegrationInstance(
        name="Global OpenSCAD",
        integration_type="openscad",
        adapter_type="openscad",
        execution_agent_id=uuid4(),
        config={"binary_path": "/usr/bin/openscad"},
    )
    personal_agent = IntegrationInstance(
        name="Personal OpenSCAD",
        integration_type="openscad",
        adapter_type="openscad",
        execution_agent_id=uuid4(),
        config={"binary_path": "/usr/bin/openscad"},
    )
    instance = IntegrationSettings(instances=[global_agent])
    personal = UserIntegrationSettings(instances=[personal_agent])
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=instance))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(return_value=personal))
    db = AsyncMock(spec=AsyncSession)
    db.scalars.return_value = type("Rows", (), {"all": lambda _self: []})()

    effective = await load_effective_integrations(db, user_id)

    assert effective.instances == []


async def test_effective_integrations_excludes_globally_disabled_adapters(
    monkeypatch,
) -> None:
    user_id = uuid4()
    global_item = IntegrationInstance(
        name="Global Search",
        integration_type="search",
        adapter_type="searxng",
    )
    personal_item = IntegrationInstance(
        name="Personal Search",
        integration_type="search",
        adapter_type="searxng",
    )
    instance = IntegrationSettings(
        instances=[global_item],
        adapter_availability={"searxng": "disabled"},
    )
    personal = UserIntegrationSettings(instances=[personal_item])
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=instance))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(return_value=personal))

    effective = await load_effective_integrations(
        AsyncMock(spec=AsyncSession), user_id
    )

    assert effective.instances == []
    assert personal.instances == [personal_item]


async def test_effective_ai_ignores_legacy_personal_default(monkeypatch) -> None:
    user_id = uuid4()
    provider = LlmProviderConfig(name="Instance", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="instance")
    instance = AiSettings(providers=[provider], models=[model])
    personal = UserAiSettings(default_model_id=uuid4())
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=instance))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(side_effect=lambda _db, model, **_kw: personal if model is UserAiSettings else None))

    effective = await load_effective_ai(AsyncMock(spec=AsyncSession), user_id)

    assert effective.default_model_id == model.id


async def test_effective_tools_uses_personal_snapshot_with_instance_web_access_limits(
    monkeypatch,
) -> None:
    instance = ToolSettings(
        public_permissions={"policies": {"filesystem.read": "ask"}},
        resource_labels={"shared": {"project-id": "searxng"}},
        web_access=WebAccessSettings(
            user_agent="Instance Agent",
            max_response_mib=7,
            public_addresses_only=False,
        ),
    )
    personal = UserToolSettings(
        public_permissions={"policies": {"personal": "auto"}},
        web_search={"primaryProvider": "searxng:personal"},
        default_calendar={
            "integrationId": uuid4(),
            "calendarKey": "calendar:" + "a" * 32,
        },
        resource_labels={"personal": {"calendar:test": "Privat"}, "shared": {"project-id": "Old name"}},
        web_access=WebAccessSettings(
            user_agent="Personal Agent",
            max_response_mib=99,
            public_addresses_only=True,
        ),
    )
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=instance))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(side_effect=lambda _db, model, **_kwargs: personal if model is UserToolSettings else None))

    effective = await load_effective_tools(AsyncMock(spec=AsyncSession), uuid4())

    assert effective.permissions.policies == {"filesystem.read": "ask"}
    assert effective.web_access.user_agent == "Instance Agent"
    assert effective.web_access.max_response_mib == 7
    assert effective.web_access.public_addresses_only is False
    assert effective.web_search.primary_provider == "no_key"
    assert effective.default_calendar == personal.default_calendar
    assert effective.resource_labels == {
        "shared": {"project-id": "searxng"}, "personal": {"calendar:test": "Privat"},
    }


async def test_effective_tools_keeps_personal_integration_defaults_when_tool_snapshot_is_off(
    monkeypatch,
) -> None:
    integration_id = uuid4()
    instance = ToolSettings(
        personal_settings_enabled=False,
        public_permissions={
            "policies": {"filesystem.delete": "ask"},
            "integrationPolicies": {str(integration_id): {"mail.read": "ask"}},
        },
    )
    personal = UserToolSettings(
        personal_tool_defaults_enabled=False,
        public_permissions={
            "policies": {"filesystem.delete": "auto"},
            "integrationPolicies": {
                str(integration_id): {"mail.read": "disabled"}
            },
        },
    )
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=instance))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(side_effect=lambda _db, model, **_kwargs: personal if model is UserToolSettings else None))

    effective = await load_effective_tools(AsyncMock(spec=AsyncSession), uuid4())

    assert effective.permissions.policies == {"filesystem.delete": "ask"}
    assert effective.permissions.integration_policies == {}


def test_personal_integration_tool_settings_do_not_snapshot_policies() -> None:
    instance = ToolSettings(
        public_permissions={
            "policies": {"filesystem.delete": "auto"},
            "integrationPolicies": {"shared": {"mail.read": "ask"}},
        },
        private_permissions={
            "integrationPolicies": {"shared": {"mail.read": "disabled"}}
        },
        web_search={"primaryProvider": "searxng:instance"},
    )

    personal = personal_integration_tool_settings(instance)

    assert set(personal.model_dump()) == {"resource_labels", "default_calendar", "default_task_list", "web_search"}
    assert personal.web_search.primary_provider == "no_key"


async def test_scoped_integration_management_enforces_admin_and_personal_gates(monkeypatch) -> None:
    user = type("User", (), {"id": uuid4(), "is_admin": False})()
    integrations = IntegrationSettings(
        adapter_availability={"caldav": "instance_only"},
    )

    async def load(_db, settings_type, **_kwargs):
        return integrations

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(ApiError, match="auth.adminRequired"):
        await _load_scoped_integrations(
            db, "instance", user, mutation=True
        )
    with pytest.raises(ApiError, match="auth.adminRequired"):
        await _load_scoped_integrations(db, "instance", user)
    with pytest.raises(ApiError, match="integration.personalAdapterDisabled"):
        await _load_scoped_integrations(
            db, "personal", user, adapter_type="caldav"
        )


async def test_settings_store_can_load_absent_optional_row_and_delete_scope() -> None:
    user_id = uuid4()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None

    assert (
        await SettingsStore.load_optional(db, UserToolSettings, user_id=user_id) is None
    )
    await SettingsStore.delete(db, UserToolSettings, user_id=user_id)

    delete_statement = db.execute.await_args_list[-1].args[0]
    assert "DELETE FROM settings" in str(delete_statement)
    assert user_id in delete_statement.compile().params.values()
