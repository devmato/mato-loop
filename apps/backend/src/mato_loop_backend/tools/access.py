from __future__ import annotations

from mato_loop_backend.permissions.policy import (
    PermissionDecision,
    PermissionRequest,
    _project_session_path_scope,
)
from mato_loop_backend.settings import (
    AgentProfileUnavailable,
    ToolSettings,
    TrustLevel,
    load_agent_profile_tools,
    load_effective_tools,
)
from mato_loop_backend.tools.context import ToolContext as ToolContext


async def allow(context: ToolContext, request: PermissionRequest) -> bool:
    if request.policy_integration_id is not None:
        return await context.ask(request) == PermissionDecision.ALLOW
    options = getattr(context, "bound_options", {})
    integration_ids = set(options.get("integration_ids", ()))
    if options.get("integration_id") is not None:
        integration_ids.add(options["integration_id"])

    async def selected() -> bool:
        if not integration_ids:
            return True
        current = await context.current_integrations()
        return integration_ids <= {str(item.id) for item in current.instances if item.enabled}

    # Integration handlers can request additional approvals.
    allowed = (
        await selected()
        and await context.ask(request) == PermissionDecision.ALLOW
        and await selected()
    )
    if not allowed or request.resource_type != "project_session_path" or context.model_trust_level != TrustLevel.PUBLIC:
        return allowed

    from mato_loop_backend.tools.sessions import session_read_permission

    scope = _project_session_path_scope(request.resource_scope)
    if scope is None:
        return False
    target, permission = await session_read_permission(context, scope[1])
    if target is None or target.project_id != context.session.project_id:
        return False
    # Reload source trust after any ordinary file approval wait.
    if permission.capability == "session.read_sensitive":
        if await context.ask(permission) != PermissionDecision.ALLOW:
            return False
        # Revalidate file policy and ownership if the sensitive approval also waited.
        return await context.ask_handler(request, check_only=True) in {
            PermissionDecision.ALLOW, PermissionDecision.ASK,
        } and await selected()
    return True


async def profile_tool_settings(context: ToolContext) -> ToolSettings:
    trust_level = getattr(context, "model_trust_level", TrustLevel.PUBLIC)
    try:
        settings, _profile = await load_agent_profile_tools(
            context.db,
            context.session.user_id,
            getattr(context, "agent_profile_id", None)
            or getattr(context.session, "agent_profile_id", None),
            trust_level,
        )
    except AgentProfileUnavailable:
        settings = await load_effective_tools(
            context.db, context.session.user_id, trust_level
        )
        settings._agent_profile_unavailable = True
    from mato_loop_backend.agent.subagents import apply_delegation_ceiling

    return apply_delegation_ceiling(settings, context.tool_settings._delegation_ceiling)
