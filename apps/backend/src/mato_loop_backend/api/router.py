from fastapi import APIRouter

from mato_loop_backend.api.routes.agent_identity import router as agent_identity_router
from mato_loop_backend.api.routes.agent_profiles import router as agent_profiles_router
from mato_loop_backend.api.routes.appearance import router as appearance_router
from mato_loop_backend.api.routes.approvals import router as approvals_router
from mato_loop_backend.api.routes.auth import router as auth_router
from mato_loop_backend.api.routes.automations import router as automations_router
from mato_loop_backend.api.routes.browser_integrations import router as browser_integrations_router
from mato_loop_backend.api.routes.browsers import router as browsers_router
from mato_loop_backend.api.routes.channels import router as channels_router
from mato_loop_backend.api.routes.commands import router as commands_router
from mato_loop_backend.api.routes.events import router as events_router
from mato_loop_backend.api.routes.execution_agents import router as execution_agents_router
from mato_loop_backend.api.routes.general_settings import router as general_settings_router
from mato_loop_backend.api.routes.git import router as git_router
from mato_loop_backend.api.routes.health import router as health_router
from mato_loop_backend.api.routes.instance_limits import router as instance_limits_router
from mato_loop_backend.api.routes.integrations import router as integrations_router
from mato_loop_backend.api.routes.mail import router as mail_router
from mato_loop_backend.api.routes.mcp import callback_router as mcp_callback_router
from mato_loop_backend.api.routes.mcp import router as mcp_router
from mato_loop_backend.api.routes.media import router as media_router
from mato_loop_backend.api.routes.memories import router as memories_router
from mato_loop_backend.api.routes.model_defaults import router as model_defaults_router
from mato_loop_backend.api.routes.notifications import router as notifications_router
from mato_loop_backend.api.routes.personal_variables import router as personal_variables_router
from mato_loop_backend.api.routes.project_coding import router as project_coding_router
from mato_loop_backend.api.routes.projects import router as projects_router
from mato_loop_backend.api.routes.providers import router as providers_router
from mato_loop_backend.api.routes.questions import router as questions_router
from mato_loop_backend.api.routes.search import router as search_router
from mato_loop_backend.api.routes.session_templates import router as session_templates_router
from mato_loop_backend.api.routes.sessions import router as sessions_router
from mato_loop_backend.api.routes.skills import router as skills_router
from mato_loop_backend.api.routes.tbs_desk import router as tbs_desk_router
from mato_loop_backend.api.routes.terminals import router as terminals_router
from mato_loop_backend.api.routes.tool_settings import router as tool_settings_router
from mato_loop_backend.api.routes.user_profile import router as user_profile_router
from mato_loop_backend.api.routes.users import router as users_router
from mato_loop_backend.api.routes.workspace import router as workspace_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(events_router, tags=["events"])
api_router.include_router(auth_router, tags=["auth"])
api_router.include_router(automations_router, tags=["automations"])
api_router.include_router(channels_router, tags=["channels"])
api_router.include_router(commands_router, tags=["commands"])
api_router.include_router(git_router, tags=["git"])
api_router.include_router(users_router, tags=["users"])
api_router.include_router(execution_agents_router, tags=["execution-agents"])
api_router.include_router(instance_limits_router, tags=["instance-limits"])
api_router.include_router(general_settings_router, tags=["settings"])
api_router.include_router(appearance_router, tags=["settings"])
api_router.include_router(agent_identity_router, tags=["agent-identity"])
api_router.include_router(agent_profiles_router, tags=["agent-profiles"])
api_router.include_router(model_defaults_router, tags=["model-defaults"])
api_router.include_router(session_templates_router, tags=["session-templates"])
api_router.include_router(providers_router, tags=["providers"])
api_router.include_router(projects_router, tags=["projects"])
api_router.include_router(project_coding_router, tags=["project-coding"])
api_router.include_router(integrations_router, tags=["integrations"])
api_router.include_router(browser_integrations_router, tags=["integrations"])
api_router.include_router(browsers_router, tags=["browsers"])
api_router.include_router(mail_router, tags=["mail"])
api_router.include_router(mcp_router, tags=["mcp"])
api_router.include_router(mcp_callback_router, tags=["mcp"])
api_router.include_router(memories_router, tags=["memories"])
api_router.include_router(notifications_router, tags=["notifications"])
api_router.include_router(personal_variables_router, tags=["personal-variables"])
api_router.include_router(media_router, tags=["media"])
api_router.include_router(search_router, tags=["search"])
api_router.include_router(sessions_router, tags=["sessions"])
api_router.include_router(skills_router, tags=["skills"])
api_router.include_router(approvals_router, tags=["approvals"])
api_router.include_router(questions_router, tags=["questions"])
api_router.include_router(workspace_router, tags=["workspace"])
api_router.include_router(tool_settings_router, tags=["tools"])
api_router.include_router(terminals_router, tags=["terminals"])
api_router.include_router(tbs_desk_router, tags=["tbs-desk"])
api_router.include_router(user_profile_router, tags=["user-profile"])
