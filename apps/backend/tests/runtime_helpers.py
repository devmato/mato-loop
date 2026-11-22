"""Shared scripted-provider setup for runtime orchestration tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.context import ContextManager
from mato_loop_backend.agent.projection import ContextProjection, message_dict
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.automations import AutomationService
from mato_loop_backend.events import run_events
from mato_loop_backend.models import (
    AgentRun,
    ChatSession,
    Message,
    MessageAttachment,
    WebSearchPolicy,
)
from mato_loop_backend.providers.base import ProviderResult, ToolCall
from mato_loop_backend.providers.service import ProviderService
from mato_loop_backend.settings import (
    AgentIdentitySettings,
    AgentRuntimeSettings,
    AiProviderSettings,
    AiSettings,
    InstanceLimits,
    InstanceSkillsSettings,
    IntegrationSettings,
    MemorySettings,
    SettingsStore,
    SkillsSettings,
    ToolSettings,
    UserProfileSettings,
)
from mato_loop_backend.settings.models import (
    LlmProviderConfig,
    ModelConfig,
    ModelDefaultsSettings,
    TrustLevel,
    UserPreferences,
)
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.tools.contracts import ToolResult

SOURCE_ID = "src-test"
SOURCE_URL = "https://example.test/source"
SOURCE_TITLE = "Quelle"
SOURCE_CITATION = f"[{SOURCE_TITLE}]({SOURCE_URL})"


def _search_call(call_id: str, query: str) -> ToolCall:
    return ToolCall(id=call_id, name="web_search", arguments={"query": query})


def _search_result(*, empty: bool = False) -> ToolResult:
    if empty:
        return ToolResult.json([], metadata={"resultCount": 0, "sources": []})
    source = {
        "sourceId": SOURCE_ID,
        "title": SOURCE_TITLE,
        "url": SOURCE_URL,
        "kind": "search",
    }
    return ToolResult.json(
        [{**source, "snippet": "Aktuelle Information"}],
        metadata={"resultCount": 1, "sources": [source]},
    )


async def _runtime_setup(
    monkeypatch,
    results: list[ProviderResult],
    *,
    max_tool_calls: int | None = 25,
    search_policy: WebSearchPolicy = WebSearchPolicy.REQUIRED,
    active_supports_image_input: bool = False,
    vision_fallback: bool = False,
    vision_fallback_private: bool = False,
    tool_result: ToolResult | None = None,
    skills: SkillsSettings | list | None = None,
    streamed_contents: list[str | None] | None = None,
    integration_settings: IntegrationSettings | None = None,
    execution_agent_id=None,
    execution_operations: tuple[str, ...] = (),
    user_image: bool = False,
    user_image_count: int = 1,
    downloaded_image: bool = False,
    user_content: str = "Aktuelle Frage",
    user_trust_level: TrustLevel | None = None,
    reasoning_effort: str | None = None,
    model_id: str = "local-model",
    safety_reserve_tokens: int = 4_096,
    input_budget_tokens: int = 1_000,
    integration_tool_loading: str = "all",
    scripted_tools_enabled: bool = False,
):
    provider = LlmProviderConfig(
        name="Local",
        adapter_type="openai-compatible",
        config={"base_url": "https://llm.example.test/v1"},
    )
    model = ModelConfig(
        provider_id=provider.id,
        model_id=model_id,
        supports_image_input=active_supports_image_input,
        scripted_tools_enabled=scripted_tools_enabled,
    )
    vision_model = (
        ModelConfig(
            provider_id=provider.id,
            model_id="vision-model",
            supports_image_input=True,
            supports_tools=False,
            trust_level=(TrustLevel.PRIVATE if vision_fallback_private else None),
        )
        if vision_fallback
        else None
    )
    ai_settings = AiSettings(
        providers=[provider],
        models=[item for item in (model, vision_model) if item is not None],
    )
    model_defaults = ModelDefaultsSettings(default_model_id=model.id, vision_model_id=vision_model.id if vision_model else None)
    runtime_settings = AgentRuntimeSettings(safety_reserve_tokens=safety_reserve_tokens)
    session = ChatSession(
        id=uuid4(),
        user_id=uuid4(),
        title="Test",
        model_config_id=model.id,
        memory_mode="disabled",
        web_search_policy=search_policy.value,
        reasoning_effort=reasoning_effort,
    )
    run = AgentRun(
        id=uuid4(),
        session_id=session.id,
        status="running",
        execution_agent_id=execution_agent_id,
        execution_operations=list(execution_operations),
    )
    user = Message(
        id=uuid4(),
        session_id=session.id,
        role="user",
        content=user_content,
        provider_data={"_matoloop": {"maxToolCallsByRun": {str(run.id): max_tool_calls}}},
        trust_level=user_trust_level.value if user_trust_level is not None else None,
    )
    run.input_message_id = user.id
    if isinstance(skills, list):
        from skill_helpers import store_skill

        import mato_loop_backend.agent.loop as skill_loop
        from mato_loop_backend.files.service import RootFileService
        from mato_loop_backend.files.storage import SessionStorage
        from mato_loop_backend.skill_storage import SkillStorage

        skills = SkillsSettings(skills=[store_skill(skill, session.user_id) for skill in skills])
        storage = SessionStorage(SkillStorage().root.parent / "runtime-sessions")
        storage.provision(session.id)
        monkeypatch.setattr(skill_loop, "RootFileService", lambda session_id, **kwargs: RootFileService(session_id, session_storage=storage))
    records = [user]
    if user_image:
        user.attachments = [
            MessageAttachment(
                id=uuid4(),
                message_id=user.id,
                position=position,
                kind="image",
                category="user_upload",
                purpose="attachment",
                mime_type="image/png",
                size_bytes=100,
                sha256="a" * 64,
                file_path=f"attachments/{uuid4()}/reference.png",
                filename=f"reference-{position}.png",
            )
            for position in range(user_image_count)
        ]
    if downloaded_image:
        tool_message = Message(
            id=uuid4(),
            session_id=session.id,
            role="tool",
            content='{"attachments":[{"filename":"drawing-page-2.jpg","pageNumber":2}]}',
            tool_call_id="fetch",
            trust_level=TrustLevel.PUBLIC.value,
        )
        tool_message.attachments = [
            MessageAttachment(
                id=uuid4(),
                message_id=tool_message.id,
                position=0,
                kind="image",
                category="downloaded",
                purpose="artifact",
                mime_type="image/jpeg",
                size_bytes=100,
                sha256="b" * 64,
                file_root_id=session.id,
                file_id=uuid4(),
                file_revision=1,
                file_path="drawing-page-2.jpg",
                filename="drawing-page-2.jpg",
                metadata_json={"pageNumber": 2},
            )
        ]
        records.append(tool_message)
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = session
    if execution_agent_id is not None:
        db.scalars.return_value = SimpleNamespace(all=lambda: [execution_agent_id])

    async def load_settings(_db, settings_type, **_kwargs):
        return {
            AiProviderSettings: ai_settings,
            AgentIdentitySettings: AgentIdentitySettings(),
            AgentRuntimeSettings: runtime_settings,
            InstanceLimits: InstanceLimits(),
            InstanceSkillsSettings: InstanceSkillsSettings(),
            MemorySettings: MemorySettings(enabled=False),
            IntegrationSettings: integration_settings or IntegrationSettings(),
            SkillsSettings: skills or SkillsSettings(),
            ToolSettings: ToolSettings(
                public_permissions={"policies": {"skills.load": "auto"}}
            ),
            UserProfileSettings: UserProfileSettings(),
            UserPreferences: UserPreferences(),
        }[settings_type]

    async def load_optional(_db, settings_type, **_kwargs):
        from mato_loop_backend.settings.models import GENERAL_PROFILE_ID, AgentProfilesSettings

        if settings_type is AgentProfilesSettings:
            # Existing scripted calls test execution workflows, not discovery.
            return AgentProfilesSettings(builtin_access_overrides={GENERAL_PROFILE_ID: {
                "integration_tool_loading": integration_tool_loading,
            }})
        return model_defaults if settings_type is ModelDefaultsSettings else None

    projections = []

    async def build_context(*_args, **kwargs):
        projections.append(kwargs)
        messages = [
            {"role": "system", "content": kwargs["system_prompt"]},
            *[
                message_dict(record, include_attachment_markers=False)
                for record in records
            ],
        ]
        return ContextProjection(
            messages=messages,
            estimated_input_tokens=await kwargs["estimate_request"](messages),
            input_budget_tokens=input_budget_tokens,
        )

    requests = []
    queue = list(results)
    streamed_queue = list(streamed_contents) if streamed_contents is not None else None

    async def complete(_db, **kwargs):
        requests.append(kwargs)
        result = queue.pop(0)
        streamed_content = (
            streamed_queue.pop(0) if streamed_queue is not None else result.content
        )
        if streamed_content and kwargs.get("on_delta"):
            await kwargs["on_delta"](streamed_content)
        return result

    events = []

    async def publish(_run_id, event):
        events.append(event)

    monkeypatch.setattr(SettingsStore, "load", load_settings)
    monkeypatch.setattr(SettingsStore, "load_optional", load_optional)
    monkeypatch.setattr(AgentRuntime, "_records", AsyncMock(return_value=records))
    monkeypatch.setattr(AutomationService, "session_definitions", AsyncMock(return_value=[]))
    monkeypatch.setattr(AgentRuntime, "_finalize_run_files", AsyncMock())
    monkeypatch.setattr(ContextManager, "build", build_context)
    monkeypatch.setattr(ProviderService, "complete", staticmethod(complete))
    monkeypatch.setattr(
        tool_registry,
        "_execute_definition",
        AsyncMock(return_value=tool_result or _search_result()),
    )
    monkeypatch.setattr(run_events, "publish", publish)
    return db, run, requests, projections, events


def _assistant_messages(db) -> list[Message]:
    return [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message) and call.args[0].role == "assistant"
    ]


def _tool_messages(db) -> list[Message]:
    return [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message) and call.args[0].role == "tool"
    ]
