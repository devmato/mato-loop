from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from mato_loop_backend.settings.models import (
    BUILD_PROFILE_ID,
    GENERAL_PROFILE_ID,
    PLAN_PROFILE_ID,
    SYSADMIN_PROFILE_ID,
    ChatModelOverride,
    SettingsDocument,
    SettingsScope,
)

DEFAULT_TEMPLATE_ID = UUID("00000000-0000-4000-8000-000000000101")
CODING_TEMPLATE_ID = UUID("00000000-0000-4000-8000-000000000102")


class SessionProfileConfig(BaseModel):
    model_config = ChatModelOverride.model_config
    agent_ids: list[UUID] | None = Field(default=None, min_length=1)
    agent_models: dict[UUID, ChatModelOverride] = Field(default_factory=dict)
    message_size: Literal["standard", "small"] = "standard"
    worktrees_enabled: bool = False

    @model_validator(mode="after")
    def validate_agents(self):
        if self.agent_ids is not None and (len(set(self.agent_ids)) != len(self.agent_ids) or set(self.agent_models) - set(self.agent_ids)):
            raise ValueError("agent overrides must reference unique selected agents")
        return self


class SessionProfileSnapshot(SessionProfileConfig):
    template_id: UUID | None = None


class SessionTemplate(SessionProfileConfig, ChatModelOverride):
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=80)
    agent_ids: list[UUID] = Field(min_length=1)
    start_agent_id: UUID
    memory_mode: Literal["full", "read_only", "disabled"] = "full"

    def snapshot(self) -> SessionProfileSnapshot:
        return SessionProfileSnapshot(template_id=self.id, agent_ids=self.agent_ids, agent_models=self.agent_models, message_size=self.message_size, worktrees_enabled=self.worktrees_enabled).model_copy(deep=True)

    @model_validator(mode="after")
    def validate_start(self):
        if self.start_agent_id not in self.agent_ids:
            raise ValueError("the start agent must be selected")
        # Older stored Coding templates must retain worktree support.
        if self.id == CODING_TEMPLATE_ID and "worktrees_enabled" not in self.model_fields_set:
            self.worktrees_enabled = True
        return self


class SessionTemplatesSettings(SettingsDocument):
    settings_key = "session-templates"
    personal_settings_enabled: bool = True
    templates: list[SessionTemplate] = Field(default_factory=lambda: [
        SessionTemplate(id=DEFAULT_TEMPLATE_ID, name="Default", agent_ids=[GENERAL_PROFILE_ID, SYSADMIN_PROFILE_ID], start_agent_id=GENERAL_PROFILE_ID),
        SessionTemplate(id=CODING_TEMPLATE_ID, name="Coding", agent_ids=[PLAN_PROFILE_ID, BUILD_PROFILE_ID], start_agent_id=PLAN_PROFILE_ID, message_size="small", worktrees_enabled=True),
    ])


class UserSessionTemplatesSettings(SettingsDocument):
    settings_key = "session-templates"
    settings_scope = SettingsScope.USER
    templates: list[SessionTemplate] = Field(default_factory=list)
