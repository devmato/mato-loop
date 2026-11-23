from __future__ import annotations

import asyncio
import hashlib
from uuid import uuid4

import mato_loop_backend.files.access as files_access
import mato_loop_backend.permissions.resolution as permissions_resolution
import mato_loop_backend.tools.access as tools_access
from mato_loop_backend.execution.client import ExecutionClientError
from mato_loop_backend.files.service import RootFileError, RootFileService
from mato_loop_backend.models import AgentRun
from mato_loop_backend.permissions.policy import (
    skill_edit_permission,
    skill_load_permission,
    skill_write_permission,
)
from mato_loop_backend.settings import ToolExecutionPolicy
from mato_loop_backend.skill_packages import SkillError, parse_skill_md, skill_path
from mato_loop_backend.skill_resources import (
    materialize,
    prepare_computer,
    read_draft,
    session_package_path,
)
from mato_loop_backend.skills import SkillService
from mato_loop_backend.tools.actions import _SESSION_FILES_READ, _SKILL_LOAD, _SKILL_WRITE
from mato_loop_backend.tools.arguments import (
    EditSkillArguments,
    LoadSkillArguments,
    SaveSkillArguments,
)
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.contracts import ToolDefinition, ToolResult


def session_files(context: ToolContext) -> RootFileService:
    return RootFileService(
        context.root_files.session_id, project_id=context.root_files.project_id,
        session_storage=context.root_files.session_storage,
        user_id=context.root_files.user_id,
        user_quota_bytes=context.root_files.user_quota_bytes,
        max_editable_text_bytes=context.root_files.max_editable_text_bytes,
    )


async def _save_skill(arguments: SaveSkillArguments, context: ToolContext) -> ToolResult:
    scope = arguments.scope
    current_skill = None
    try:
        await SkillService.require_write(context.db, context.session.user_id, scope)
        if arguments.skill_id is not None:
            current_skill = await SkillService.get(context.db, context.session.user_id, arguments.skill_id, scope)
            if current_skill.revision != arguments.expected_revision:
                raise SkillError("skill.changed")
    except SkillError as exc:
        return ToolResult.error(exc.code)
    context.tool_settings = await tools_access.profile_tool_settings(context)
    policy = permissions_resolution.tool_execution_policy(context.tool_settings, "skills.write", ToolExecutionPolicy.ASK)
    if policy == ToolExecutionPolicy.DISABLED:
        return ToolResult.error("tool.unavailable")
    try:
        path = skill_path(arguments.path)
        executable = [skill_path(path) for path in arguments.executable_paths]
        request = files_access.filesystem_permission(
            context, root="session", path=path, capability="filesystem.read",
            action_kind="save_skill", action_label="Skill-Paket lesen",
        )
        if not await tools_access.allow(context, request):
            return ToolResult.error("permission.denied")
        package = await asyncio.to_thread(read_draft, context.root_files, path, executable)
        if not set(executable) <= package.files.keys():
            raise SkillError("skill.draftInvalid")
        # Publish a review copy outside the run's file leases, so the owner can edit it.
        draft_id = uuid4()
        path = f"skill-drafts/{draft_id}/{package.name}"
        files = session_files(context)
        await asyncio.to_thread(materialize, files, package, path)
    except (SkillError, RootFileError) as exc:
        return ToolResult.error(exc.code)
    metadata, instructions = parse_skill_md(package.files["SKILL.md"].content)
    details = {
        "skillPreview": {"name": package.name, "description": metadata["description"], "instructions": instructions, "scope": scope, "files": sorted(package.files)},
        "skillDraft": {"path": path, "executablePaths": executable},
        "draftFile": {"root": "session", "path": f"{path}/SKILL.md", "filename": "SKILL.md", "sizeBytes": len(package.files["SKILL.md"].content), "initialRevision": package.revision, "fileRevision": hashlib.sha256(package.files["SKILL.md"].content).hexdigest()},
    }
    if current_skill:
        details["skillTarget"] = {"id": str(current_skill.id), "name": current_skill.name, "scope": scope}
    approved = False
    if policy == ToolExecutionPolicy.ASK:
        context.approval_resolution.clear()
        permission = (
            skill_edit_permission(current_skill.id, current_skill.name, approval_details=details, scope=scope)
            if current_skill else skill_write_permission(context.session.user_id, package.name, approval_details=details, scope=scope)
        )
        if not await tools_access.allow(context, permission):
            return ToolResult.error("permission.denied")
        approved = True
    context.tool_settings = await tools_access.profile_tool_settings(context)
    current_policy = permissions_resolution.tool_execution_policy(context.tool_settings, "skills.write", ToolExecutionPolicy.ASK)
    if current_policy == ToolExecutionPolicy.DISABLED or (current_policy == ToolExecutionPolicy.ASK and not approved):
        return ToolResult.error("tool.unavailable")
    try:
        reviewed = await asyncio.to_thread(read_draft, files, path, executable)
        if reviewed.revision != context.approval_resolution.get("expected_revision", package.revision):
            return ToolResult.error("file.changed")
        skill = await SkillService.save_package(
            context.db, context.session.user_id, current_skill.id if current_skill else draft_id,
            reviewed, scope=scope, create=current_skill is None,
            expected_revision=current_skill.revision if current_skill else None,
        )
    except (SkillError, RootFileError) as exc:
        return ToolResult.error(exc.code)
    return ToolResult.json({"id": str(skill.id), "name": skill.name, "scope": scope, "updated": current_skill is not None, "effectiveFrom": "next_run"})


async def _edit_skill(arguments: EditSkillArguments, context: ToolContext) -> ToolResult:
    skill = context.skill_state.catalog.get(arguments.skill_id)
    if skill is None:
        return ToolResult.error("skill.notFound")
    if not skill.editable:
        return ToolResult.error("skill.notEditable")
    # Resolve the requested ownership scope; a personal override and the global original
    # may intentionally have the same stable default-skill ID.
    try:
        await SkillService.require_write(context.db, context.session.user_id, arguments.scope)
        current = await SkillService.get(context.db, context.session.user_id, skill.id, arguments.scope)
        if current.revision != skill.revision or current.owner_id != skill.owner_id:
            return ToolResult.error("skill.changed")
        context.tool_settings = await tools_access.profile_tool_settings(context)
        if permissions_resolution.tool_execution_policy(context.tool_settings, "skills.write", ToolExecutionPolicy.ASK) == ToolExecutionPolicy.DISABLED:
            return ToolResult.error("tool.unavailable")
        if not await tools_access.allow(context, skill_load_permission(context.session.user_id, skill.name)):
            return ToolResult.error("permission.denied")
        context.tool_settings = await tools_access.profile_tool_settings(context)
        if any(permissions_resolution.tool_execution_policy(context.tool_settings, capability, default) == ToolExecutionPolicy.DISABLED for capability, default in (("skills.load", ToolExecutionPolicy.AUTO), ("skills.write", ToolExecutionPolicy.ASK))):
            return ToolResult.error("tool.unavailable")
        await SkillService.require_write(context.db, context.session.user_id, arguments.scope)
        package = await SkillService.package(current)
        path = f"skill-drafts/{uuid4()}/{package.name}"
        await asyncio.to_thread(materialize, context.root_files, package, path)
    except (SkillError, RootFileError) as exc:
        return ToolResult.error(exc.code)
    return ToolResult.json({
        "id": str(skill.id), "scope": arguments.scope, "path": path,
        "expected_revision": current.revision,
        "executable_paths": [path for path, entry in package.files.items() if entry.executable],
        "files": sorted(package.files),
        "next": "Read and edit these session files with normal file tools, then call save_skill with this ID, scope and revision.",
    })


async def _load_skill(arguments: LoadSkillArguments, context: ToolContext) -> ToolResult:
    skill = context.skill_state.active.get(arguments.skill_id) or context.skill_state.catalog.get(arguments.skill_id)
    if skill is None:
        return ToolResult.error("skill.notFound")
    context.tool_settings = await tools_access.profile_tool_settings(context)
    policy = permissions_resolution.tool_execution_policy(context.tool_settings, "skills.load", ToolExecutionPolicy.AUTO)
    if policy == ToolExecutionPolicy.DISABLED:
        return ToolResult.error("tool.unavailable")
    approved = False
    if policy == ToolExecutionPolicy.ASK:
        if not await tools_access.allow(context, skill_load_permission(context.session.user_id, skill.name)):
            return ToolResult.error("permission.denied")
        approved = True
    context.tool_settings = await tools_access.profile_tool_settings(context)
    current_policy = permissions_resolution.tool_execution_policy(context.tool_settings, "skills.load", ToolExecutionPolicy.AUTO)
    if current_policy == ToolExecutionPolicy.DISABLED or (current_policy == ToolExecutionPolicy.ASK and not approved):
        return ToolResult.error("tool.unavailable")
    try:
        # Resolve the current revision after any approval wait.
        references, _ = await SkillService.effective_references(context.db, context.session.user_id)
        reference = next((item for item, owner in references if item.id == skill.id and owner == skill.owner_id), None)
        if reference is None:
            return ToolResult.error("skill.notFound")
        if reference.revision != skill.revision:
            skill = await SkillService._definition(reference, skill.owner_id)
        loaded = skill.model_copy(deep=True)
        if loaded.files_path is None or arguments.on_computer:
            package = await SkillService.package(skill)
        if loaded.files_path is None:
            loaded.files_path = await asyncio.to_thread(materialize, session_files(context), package, session_package_path(context.run_id, package))
        if arguments.on_computer:
            client = context.execution_client
            if client is None:
                return ToolResult.error("file.workspaceNotSelected")
            run = await context.db.get(AgentRun, context.run_id)
            if run is None or run.session_id != context.session.id or run.execution_agent_id != client.agent_id:
                return ToolResult.error("executionAgent.unavailable")
            loaded.computer_path = await prepare_computer(client, run, package)
            loaded.computer_id = client.agent_id
        if context.skill_state.catalog[loaded.id].revision != loaded.revision:
            context.skill_state.catalog[loaded.id] = skill
        context.skill_state.active[loaded.id] = loaded
    except (SkillError, RootFileError, ExecutionClientError) as exc:
        return ToolResult.error(exc.code)
    return ToolResult.json({
        "id": str(loaded.id), "name": loaded.name, "loaded": True,
        "revision": loaded.revision, "filesBasePath": loaded.files_path,
        "computerBasePath": loaded.computer_path if context.execution_client and loaded.computer_id == context.execution_client.agent_id else None,
        "resources": loaded.resource_paths, "effectiveFrom": "next_model_request",
    }, metadata={"skillLoad": {"ownerId": str(loaded.owner_id) if loaded.owner_id else None}})


LOAD_SKILL = ToolDefinition(
    "load_skill", "Load a matching catalog skill; call alone.",
    LoadSkillArguments, _load_skill, group="agent", label="Skill laden", icon="school", actions=(_SKILL_LOAD,),
)
SAVE_SKILL = ToolDefinition(
    "save_skill", "Publish a session-files skill directory. SKILL.md: YAML name (lowercase-hyphenated) and description, then Markdown. Updates replace all files.",
    SaveSkillArguments, _save_skill, group="agent", label="Skill speichern", icon="save", actions=(_SESSION_FILES_READ, _SKILL_WRITE),
)
EDIT_SKILL = ToolDefinition(
    "edit_skill", "Prepare a catalog skill's files for editing without activating it.",
    EditSkillArguments, _edit_skill, group="agent", label="Skill bearbeiten", icon="edit_note", actions=(_SKILL_LOAD, _SKILL_WRITE),
)
