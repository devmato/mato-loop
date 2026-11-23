"""Authorize skill content for projection at its original tool exchange."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.files.service import RootFileError, RootFileService
from mato_loop_backend.models import Message
from mato_loop_backend.permissions.policy import (
    PermissionDecision,
    PermissionRequest,
    path_permission,
    skill_load_permission,
)
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings.models import SkillDefinition, TrustLevel
from mato_loop_backend.skill_packages import MAX_FILE_BYTES
from mato_loop_backend.skills import RunSkillState, SkillService

UNAVAILABLE_SKILL_REFERENCE = '{"skillReferenceUnavailable":true}'
UNAVAILABLE_SKILL_LOAD = '{"loaded":false}'


def guarded_skill_result(row: Message) -> bool:
    host = (row.provider_data or {}).get("_matoloop", {})
    return row.role == "tool" and (
        isinstance(host.get("skillLoad"), dict) or isinstance(host.get("skillReferenceRead"), dict)
    )


def is_skill_copy_path(path: str) -> bool:
    parts = path.split("/", 4)
    if len(parts) != 5 or parts[0] != "skills":
        return False
    try:
        return all(str(UUID(part)) == part for part in parts[1:3])
    except ValueError:
        return False


def skill_instructions(skill: SkillDefinition) -> dict[str, str]:
    return {"description": skill.description, "instructions": skill.instructions}


async def prepare_skill_context(
    db: AsyncSession,
    records: list[Message],
    reference_ids: set[UUID] | None = None,
    *,
    run_id: UUID,
    user_id: UUID,
    root_files: RootFileService,
    state: RunSkillState,
    trust: TrustLevel,
    authorize: Callable[[ToolCall, PermissionRequest], Awaitable[PermissionDecision]],
    can_load: bool,
    can_read: bool,
    reuse_history: bool,
    computer_id: UUID | None,
) -> dict[UUID, str]:
    """Return projection-only replacements; never write instruction text to the transcript."""
    replacements: dict[UUID, str] = {}
    origins: dict[str, SkillDefinition] = {}
    seen: set[UUID] = set()
    load_decision: PermissionDecision | None = None
    revisions: dict[UUID, tuple[UUID | None, str]] | None = None
    candidates = []
    reads = []
    for row in records:
        if row.role != "tool" or row.session_id != root_files.session_id or row.error_code:
            continue
        host = (row.provider_data or {}).get("_matoloop", {})
        if host.get("errorCode"):
            continue
        if host.get("toolName") == "load_skill" and not host.get("scriptParent"):
            candidates.append((row, host))
        elif (host.get("toolName") == "read" and isinstance(host.get("skillReferenceRead"), dict)
              and (reference_ids is None or row.id in reference_ids)):
            reads.append((row, host))

    for row, host in reversed(candidates):
        receipt = host.get("skillLoad")
        try:
            payload = json.loads(row.content)
            skill_id = UUID(payload["id"])
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        if payload.get("loaded") is not True:
            continue
        path = payload.get("filesBasePath")
        if not isinstance(receipt, dict) or skill_id in seen:
            # Older or superseded receipts are not evidence of current availability.
            replacements[row.id] = UNAVAILABLE_SKILL_LOAD
            continue
        seen.add(skill_id)
        replacements[row.id] = UNAVAILABLE_SKILL_LOAD
        live = host.get("runId") == str(run_id)
        skill = (state.active if live else state.catalog).get(skill_id)
        if (not can_load or not (live or reuse_history) or skill is None
                or (trust == TrustLevel.PUBLIC and row.trust_level != TrustLevel.PUBLIC.value)
                or not skill.revision or payload.get("revision") != skill.revision
                or receipt.get("ownerId") != (str(skill.owner_id) if skill.owner_id else None)):
            continue
        if payload.get("computerBasePath") is not None:
            # A working copy on an Agent is valid only during its owning run and binding.
            if not (live and computer_id is not None and skill.computer_id == computer_id
                    and skill.computer_path == payload["computerBasePath"]):
                payload["computerBasePath"] = None
        parts = path.split("/") if isinstance(path, str) else []
        if len(parts) != 4 or parts[:2] != ["skills", host.get("runId")] or parts[3] != skill.name:
            continue
        if revisions is None:
            effective, _ = await SkillService.effective_references(db, user_id)
            revisions = {ref.id: (owner, ref.revision) for ref, owner in effective}
        if revisions.get(skill_id) != (skill.owner_id, skill.revision):
            continue
        if load_decision is None:
            call = ToolCall("skill-context", "load_skill", {"skill_id": str(skill_id)})
            load_decision = await authorize(call, skill_load_permission(user_id, skill.name))
        # A successful load already got any required approval for its own run.
        # On replay, ASK is not authority to disclose; a hard denial always wins.
        if load_decision != PermissionDecision.ALLOW and not (live and load_decision == PermissionDecision.ASK):
            continue
        try:
            if await asyncio.to_thread(root_files.path_kind, "session", path) != "directory":
                continue
        except RootFileError:
            continue
        origins[path] = skill
        payload["authorizedSkill"] = skill_instructions(skill)
        replacements[row.id] = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    checked: dict[str, tuple[PermissionDecision, str | None]] = {}
    for row, host in reads:
        replacements[row.id] = UNAVAILABLE_SKILL_REFERENCE
        receipt = host["skillReferenceRead"]
        path = receipt.get("path")
        if not isinstance(path, str):
            continue
        parts = path.split("/", 4)
        base, resource = "/".join(parts[:4]), parts[4] if len(parts) == 5 else ""
        skill = origins.get(base)
        if (can_read and skill and resource in skill.resource_paths
                and (trust == TrustLevel.PRIVATE or row.trust_level == TrustLevel.PUBLIC.value)):
            live = host.get("runId") == str(run_id)
            if path not in checked:
                decision = PermissionDecision.DENY
                digest = None
                try:
                    request = path_permission(root_files=root_files, root="session", capability="filesystem.read",
                                              relative_path=path, action_kind="read", action_label="Pfad lesen")
                    call = ToolCall(f"skill-reference:{row.id}", "read", {"root": "session", "path": path})
                    decision = await authorize(call, request)
                    if decision == PermissionDecision.ALLOW or (live and decision == PermissionDecision.ASK):
                        data = await asyncio.to_thread(root_files.read_bytes, "session", path, limit=MAX_FILE_BYTES)
                        if len(data) <= MAX_FILE_BYTES:
                            digest = hashlib.sha256(data).hexdigest()
                except RootFileError:
                    pass
                checked[path] = decision, digest
            decision, digest = checked[path]
            if digest is not None and digest == receipt.get("sha256") and (
                decision == PermissionDecision.ALLOW or (live and decision == PermissionDecision.ASK)
            ):
                replacements[row.id] = row.content
                continue
    return replacements
