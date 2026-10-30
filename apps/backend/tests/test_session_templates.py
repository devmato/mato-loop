from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.routes.model_defaults import get_defaults, save_defaults
from mato_loop_backend.api.routes.session_templates import delete_template, save_template
from mato_loop_backend.api.routes.sessions import SessionOptionsRequest, session_defaults
from mato_loop_backend.models import ChatSession, Project, UserAccount
from mato_loop_backend.sessions.service import SessionServiceError
from mato_loop_backend.sessions.templates import available_templates, load_template
from mato_loop_backend.settings import SettingsStore, load_effective_ai
from mato_loop_backend.settings.effective import apply_session_ai, session_agent_allowed
from mato_loop_backend.settings.models import (
    BUILD_PROFILE_ID,
    GENERAL_PROFILE_ID,
    PLAN_PROFILE_ID,
    SYSADMIN_PROFILE_ID,
    AiProviderSettings,
    LlmProviderConfig,
    ModelConfig,
    ModelDefaults,
    ModelDefaultsSettings,
    UserAiSettings,
    UserModelDefaultsSettings,
)
from mato_loop_backend.settings.session_profiles import (
    CODING_TEMPLATE_ID,
    DEFAULT_TEMPLATE_ID,
    ChatModelOverride,
    SessionProfileSnapshot,
    SessionTemplate,
    SessionTemplatesSettings,
    UserSessionTemplatesSettings,
)


@pytest.fixture
def documents(monkeypatch):
    values = {}
    async def load(_db, model, *, user_id=None, **_kwargs):
        return values.get((model, user_id), model()).model_copy(deep=True)
    async def optional(_db, model, *, user_id=None, **_kwargs):
        return await load(_db, model, user_id=user_id) if (model, user_id) in values else None
    async def save(_db, value, *, user_id=None):
        values[type(value), user_id] = value.model_copy(deep=True)
    monkeypatch.setattr(SettingsStore, "load", load)
    monkeypatch.setattr(SettingsStore, "load_optional", optional)
    monkeypatch.setattr(SettingsStore, "save", save)
    return values


async def test_template_copy_and_agent_model_resolution(documents):
    user_id = uuid4()
    global_model, personal_model, session_model, plan_model = (uuid4() for _ in range(4))
    provider = LlmProviderConfig(name="Provider", adapter_type="openai-compatible")
    documents[AiProviderSettings, None] = AiProviderSettings(providers=[provider], models=[ModelConfig(id=model_id, provider_id=provider.id, model_id=str(model_id)) for model_id in (global_model, personal_model, session_model, plan_model)])
    documents[ModelDefaultsSettings, None] = ModelDefaultsSettings(default_model_id=global_model, title_generation_model_id=global_model, reasoning_effort="low")
    documents[UserModelDefaultsSettings, user_id] = UserModelDefaultsSettings(default_model_id=personal_model)
    template = await load_template(AsyncMock(), user_id, CODING_TEMPLATE_ID)
    template.model_id = session_model
    template.agent_models[PLAN_PROFILE_ID] = ChatModelOverride(model_id=plan_model, reasoning_effort="high")
    snapshot = template.snapshot()
    session = ChatSession(agent_profile_id=PLAN_PROFILE_ID, model_config_id=template.model_id, session_profile=snapshot.model_dump(mode="json"))
    template.agent_ids.append(SYSADMIN_PROFILE_ID)
    ai = await load_effective_ai(AsyncMock(), user_id)
    plan = apply_session_ai(ai, session)
    build = apply_session_ai(ai, session, BUILD_PROFILE_ID)
    assert (ai.default_model_id, plan.default_model_id, plan.reasoning_effort, build.default_model_id, plan.title_generation_model_id, session_agent_allowed(session, SYSADMIN_PROFILE_ID), snapshot.message_size) == (personal_model, plan_model, "high", session_model, global_model, False, "small")


@pytest.mark.parametrize("session_model", [False, True])
async def test_profile_defaults_inherit_independently_below_session_choices(documents, session_model):
    user = UserAccount(id=uuid4(), username="owner", password_hash="hash", is_admin=True)
    db = AsyncMock(spec=AsyncSession)
    provider = LlmProviderConfig(name="Provider", adapter_type="openai-compatible")
    models = [ModelConfig(provider_id=provider.id, model_id=name, discovery_metadata={"reasoning_efforts": ["low", "high"], "default_reasoning_effort": "low"}) for name in ("default", "profile", "personal", "session")]
    documents[AiProviderSettings, None] = AiProviderSettings(providers=[provider], models=models)
    await save_defaults(ModelDefaults(default_model_id=models[0].id, title_generation_model_id=models[0].id, profile_models={PLAN_PROFILE_ID: ChatModelOverride(model_id=models[1].id, reasoning_effort="low")}), user, db, "instance")
    await save_defaults(ModelDefaults(default_model_id=models[2].id, profile_models={PLAN_PROFILE_ID: ChatModelOverride(reasoning_effort="high")}), user, db, "personal")
    ai = await load_effective_ai(db, user.id)
    session = ChatSession(agent_profile_id=PLAN_PROFILE_ID, model_config_id=models[3].id if session_model else None)
    selected = apply_session_ai(ai, session)
    other = apply_session_ai(ai, session, BUILD_PROFILE_ID)
    assert (selected.default_model_id, selected.reasoning_effort, other.default_model_id, selected.title_generation_model_id) == (models[3 if session_model else 1].id, "high", models[3 if session_model else 2].id, models[0].id)


async def test_profile_model_uses_its_default_when_inherited_reasoning_is_unsupported(documents):
    user = UserAccount(id=uuid4(), username="owner", password_hash="hash", is_admin=True)
    provider = LlmProviderConfig(name="Provider", adapter_type="openai-compatible")
    models = [ModelConfig(provider_id=provider.id, model_id=name, discovery_metadata={"reasoning_efforts": [effort], "default_reasoning_effort": effort}) for name, effort in (("standard", "low"), ("profile", "high"))]
    documents[AiProviderSettings, None] = AiProviderSettings(providers=[provider], models=models)
    await save_defaults(ModelDefaults(default_model_id=models[0].id, reasoning_effort="low", profile_models={PLAN_PROFILE_ID: ChatModelOverride(model_id=models[1].id)}), user, AsyncMock(spec=AsyncSession), "instance")
    selected = apply_session_ai(await load_effective_ai(AsyncMock(spec=AsyncSession), user.id), ChatSession(agent_profile_id=PLAN_PROFILE_ID))
    assert (selected.default_model_id, selected.reasoning_effort) == (models[1].id, None)


@pytest.mark.parametrize("include_choices", [False, True])
async def test_standard_model_change_revalidates_preserved_profile_reasoning(documents, include_choices):
    user = UserAccount(id=uuid4(), username="owner", password_hash="hash", is_admin=True)
    provider = LlmProviderConfig(name="Provider", adapter_type="openai-compatible")
    models = [ModelConfig(provider_id=provider.id, model_id=effort, discovery_metadata={"reasoning_efforts": [effort], "default_reasoning_effort": effort}) for effort in ("high", "low")]
    documents[AiProviderSettings, None] = AiProviderSettings(providers=[provider], models=models)
    choices = {PLAN_PROFILE_ID: ChatModelOverride(reasoning_effort="high")}
    documents[ModelDefaultsSettings, None] = ModelDefaultsSettings(default_model_id=models[0].id, profile_models=choices)
    payload = ModelDefaults(default_model_id=models[1].id, **({"profile_models": choices} if include_choices else {}))
    with pytest.raises(ApiError, match="validation.invalidChoice"):
        await save_defaults(payload, user, AsyncMock(spec=AsyncSession), "instance")


@pytest.mark.parametrize("unavailable", ["missing", "providers_disabled"])
async def test_unavailable_personal_profile_model_keeps_global_profile_fallback(documents, unavailable):
    user_id = uuid4()
    provider = LlmProviderConfig(name="Global", adapter_type="openai-compatible")
    standard, profile = [ModelConfig(provider_id=provider.id, model_id=name) for name in ("standard", "profile")]
    personal_provider = LlmProviderConfig(name="Personal", adapter_type="openai-compatible")
    personal = ModelConfig(provider_id=personal_provider.id, model_id="personal", availability="missing" if unavailable == "missing" else "available")
    documents[AiProviderSettings, None] = AiProviderSettings(providers=[provider], models=[standard, profile], personal_providers_enabled=unavailable != "providers_disabled")
    documents[UserAiSettings, user_id] = UserAiSettings(providers=[personal_provider], models=[personal])
    documents[ModelDefaultsSettings, None] = ModelDefaultsSettings(default_model_id=standard.id, profile_models={PLAN_PROFILE_ID: ChatModelOverride(model_id=profile.id)})
    documents[UserModelDefaultsSettings, user_id] = UserModelDefaultsSettings(profile_models={PLAN_PROFILE_ID: ChatModelOverride(model_id=personal.id)})
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = UserAccount(id=user_id, is_admin=False)
    selected = apply_session_ai(await load_effective_ai(db, user_id), ChatSession(agent_profile_id=PLAN_PROFILE_ID))
    assert selected.default_model_id == profile.id


@pytest.mark.parametrize("invalid", ["profile", "model", "tools", "reasoning"])
async def test_profile_defaults_validate_profile_and_chat_model(documents, invalid):
    user = UserAccount(id=uuid4(), username="owner", password_hash="hash", is_admin=True)
    provider = LlmProviderConfig(name="Provider", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="chat", supports_tools=invalid != "tools")
    documents[AiProviderSettings, None] = AiProviderSettings(providers=[provider], models=[model])
    choice = ChatModelOverride(model_id=uuid4() if invalid == "model" else model.id, reasoning_effort="high" if invalid == "reasoning" else None)
    with pytest.raises(ApiError, match={"profile": "agentProfile.selectedUnavailable", "model": "session.modelUnavailable", "tools": "session.modelUnavailable", "reasoning": "validation.invalidChoice"}[invalid]):
        await save_defaults(ModelDefaults(profile_models={uuid4() if invalid == "profile" else PLAN_PROFILE_ID: choice}), user, AsyncMock(spec=AsyncSession), "instance")


async def test_preview_resolves_the_current_draft_not_the_original_template(documents):
    user = UserAccount(id=uuid4(), username="owner", password_hash="hash")
    model_id = uuid4()
    provider = LlmProviderConfig(name="Provider", adapter_type="openai-compatible")
    documents[AiProviderSettings, None] = AiProviderSettings(providers=[provider], models=[ModelConfig(id=model_id, provider_id=provider.id, model_id="chat")])
    payload = SessionOptionsRequest(session_template_id=CODING_TEMPLATE_ID, agent_profile_id=PLAN_PROFILE_ID, memory_mode="read_only", session_profile=SessionProfileSnapshot(agent_ids=[PLAN_PROFILE_ID, BUILD_PROFILE_ID], agent_models={PLAN_PROFILE_ID: ChatModelOverride(model_id=model_id, reasoning_effort="high")}))
    result = await session_defaults(user, AsyncMock(spec=AsyncSession), payload)
    assert (result["effectiveModelId"], result["effectiveReasoningEffort"], result["memoryMode"], result["sessionProfile"]["messageSize"]) == (str(model_id), "high", "read_only", "standard")


@pytest.mark.parametrize("explicit", [None, DEFAULT_TEMPLATE_ID])
async def test_preview_inherits_project_template_unless_explicit(documents, explicit):
    user = UserAccount(id=uuid4(), username="owner", password_hash="hash")
    project = Project(id=uuid4(), user_id=user.id, default_session_template_id=CODING_TEMPLATE_ID, git_workspace_mode="worktree")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = project
    result = await session_defaults(user, db, SessionOptionsRequest(project_id=project.id, session_template_id=explicit))
    assert (result["sessionTemplateId"], result["workspaceMode"]) == (str(explicit or CODING_TEMPLATE_ID), "local" if explicit else "worktree")


@pytest.mark.parametrize("explicit", [False, True])
async def test_agent_model_fallback_skips_ineligible_models_and_preserves_explicit_choices(documents, explicit):
    provider = LlmProviderConfig(name="Provider", adapter_type="openai-compatible")
    models = [ModelConfig(provider_id=provider.id, model_id=name, **options) for name, options in [
        ("disabled", {"enabled": False}), ("plain-chat", {"supports_tools": False}),
        ("first", {}), ("second", {}),
    ]]
    documents[AiProviderSettings, None] = AiProviderSettings(providers=[provider], models=models)
    documents[ModelDefaultsSettings, None] = ModelDefaultsSettings(default_model_id=models[0].id if explicit else None)
    effective = await load_effective_ai(AsyncMock(spec=AsyncSession), uuid4())
    assert effective.default_model_id == models[0 if explicit else 2].id


@pytest.mark.parametrize("scope,allowed,admin,available", [
    ("instance", True, True, False), ("personal", True, False, True),
    ("personal", False, False, False), ("personal", False, True, True),
])
async def test_automatic_model_display_respects_scope_and_provider_gate(documents, scope, allowed, admin, available):
    user = UserAccount(id=uuid4(), username="owner", password_hash="hash", is_admin=admin)
    provider = LlmProviderConfig(name="Personal", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="chat")
    documents[AiProviderSettings, None] = AiProviderSettings(personal_providers_enabled=allowed)
    documents[UserAiSettings, user.id] = UserAiSettings(providers=[provider], models=[model])
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = user
    result = await get_defaults(user, db, scope)
    expected = str(model.id) if available else None
    assert (result["automaticModelId"], result["effective"]["defaultModelId"], result["configured"]["defaultModelId"]) == (expected, expected, None)


def test_default_templates_and_invalid_agent_selection():
    default, coding = SessionTemplatesSettings().templates
    assert [(item.name, item.agent_ids, item.start_agent_id, item.memory_mode, item.message_size, item.snapshot().worktrees_enabled) for item in (default, coding)] == [
        ("Default", [GENERAL_PROFILE_ID, SYSADMIN_PROFILE_ID], GENERAL_PROFILE_ID, "full", "standard", False),
        ("Coding", [PLAN_PROFILE_ID, BUILD_PROFILE_ID], PLAN_PROFILE_ID, "full", "small", True),
    ]
    with pytest.raises(ValidationError):
        SessionTemplate(name="Invalid", agent_ids=[PLAN_PROFILE_ID], start_agent_id=BUILD_PROFILE_ID)


@pytest.mark.parametrize("enabled", [None, False, True])
def test_stored_coding_template_defaults_worktrees_without_overriding_explicit_choice(enabled):
    data = SessionTemplatesSettings().templates[1].model_dump()
    if enabled is None:
        del data["worktrees_enabled"]
    else:
        data["worktrees_enabled"] = enabled
    assert SessionTemplate.model_validate(data).worktrees_enabled is (enabled is not False)


def test_session_message_size_rejects_unknown_values():
    with pytest.raises(ValidationError):
        SessionProfileSnapshot(message_size="compact")


@pytest.mark.parametrize("admin", [False, True])
async def test_personal_gates_and_admin_exception(documents, admin):
    user = UserAccount(id=uuid4(), username="owner", password_hash="hash", is_admin=admin)
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = user
    personal_model = uuid4()
    own = SessionTemplate(name="Own", agent_ids=[GENERAL_PROFILE_ID], start_agent_id=GENERAL_PROFILE_ID)
    documents[ModelDefaultsSettings, None] = ModelDefaultsSettings(personal_settings_enabled=False, profile_models={PLAN_PROFILE_ID: ChatModelOverride(reasoning_effort="low")})
    documents[UserModelDefaultsSettings, user.id] = UserModelDefaultsSettings(default_model_id=personal_model, profile_models={PLAN_PROFILE_ID: ChatModelOverride(reasoning_effort="high")})
    documents[SessionTemplatesSettings, None] = SessionTemplatesSettings(personal_settings_enabled=False)
    documents[UserSessionTemplatesSettings, user.id] = UserSessionTemplatesSettings(templates=[own])
    ai = await load_effective_ai(db, user.id)
    assert (ai.default_model_id, ai.profile_models[PLAN_PROFILE_ID].reasoning_effort, len(await available_templates(db, user.id))) == (personal_model if admin else None, "high" if admin else "low", 3 if admin else 2)
    if admin:
        await save_defaults(ModelDefaults(), user, db, "personal")
        await save_template(own.id, own, user, db, "personal")
    else:
        with pytest.raises(ApiError, match="modelDefaults.personalDisabled"):
            await save_defaults(ModelDefaults(), user, db, "personal")
        with pytest.raises(ApiError, match="sessionTemplate.personalDisabled"):
            await save_template(own.id, own, user, db, "personal")


async def test_template_mutations_enforce_scope_and_default_protection(documents):
    user = UserAccount(id=uuid4(), username="owner", password_hash="hash", is_admin=False)
    db = AsyncMock(spec=AsyncSession)
    template = SessionTemplatesSettings().templates[0]
    with pytest.raises(ApiError, match="auth.adminRequired"):
        await save_template(template.id, template, user, db, "instance")
    with pytest.raises(ApiError, match="validation.invalidChoice"):
        await save_template(template.id, template, user, db, "personal")
    with pytest.raises(SessionServiceError, match="sessionTemplate.notFound"):
        await load_template(db, user.id, uuid4())
    user.is_admin = True
    with pytest.raises(ApiError, match="sessionTemplate.defaultProtected"):
        await delete_template(DEFAULT_TEMPLATE_ID, user, db, "instance")


async def test_run_start_rejects_agent_outside_session_selection(monkeypatch):
    from mato_loop_backend.agent.run_contracts import AgentRunError
    from mato_loop_backend.agent.runtime import AgentRuntime

    session = ChatSession(id=uuid4(), user_id=uuid4(), agent_profile_id=PLAN_PROFILE_ID, session_profile={"agent_ids": [str(PLAN_PROFILE_ID)]})
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, None]
    monkeypatch.setattr("mato_loop_backend.sessions.archive.is_session_archived", AsyncMock(return_value=False))
    monkeypatch.setattr(AgentRuntime, "_ensure_user_active", AsyncMock())
    with pytest.raises(AgentRunError, match="agentProfile.selectedUnavailable"):
        await AgentRuntime.start(db, session, "Task", agent_profile_id_override=BUILD_PROFILE_ID)
