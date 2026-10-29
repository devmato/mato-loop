from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.execution.paths import (
    absolute_path,
    is_absolute_path,
    parent,
    session_temporary_directory,
    within,
)
from mato_loop_backend.files.service import FileRoot, RootFileError, RootFileService
from mato_loop_backend.integrations.youtube_transcript.client import canonicalize_youtube_video_url
from mato_loop_backend.models import Automation, ChatSession, ExecutionAgent, PermissionGrant
from mato_loop_backend.settings import (
    ToolExecutionPolicy,
    ToolSettings,
    TrustLevel,
)
from mato_loop_backend.web.fetch import canonicalize_url

if TYPE_CHECKING:
    from mato_loop_backend.execution.client import ShellInspectResult


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    USER_DENIED = "user_denied"


@dataclass(frozen=True, slots=True)
class PermissionDisplayItem:
    label: str
    value: str | tuple[str, ...]
    format: Literal["text", "code", "list"] = "text"


@dataclass(frozen=True, slots=True)
class SessionGrantScope:
    access: Literal["read", "read_sensitive", "write", "delete", "navigate"]
    resource_type: str
    resource_scope: str
    resource_display: str | None = None

    @property
    def capability(self) -> str:
        return "execution.directory" if self.access == "navigate" else f"category.{self.access}"


def _access_for_capability(capability: str) -> Literal["read", "read_sensitive", "write", "delete", "navigate"] | None:
    if capability == "execution.directory":
        return "navigate"
    from mato_loop_backend.integrations.registry import integration_action
    from mato_loop_backend.tools.catalog import tool_registry

    action = tool_registry.actions_by_capability.get(capability) or integration_action(capability)
    return action.category if action is not None else None


_CATEGORY_LABELS = {"read": "Lesen", "read_sensitive": "Vertrauliche Daten lesen", "write": "Lesen und Schreiben", "delete": "Löschen"}


def category_covers(granted: str, requested: str) -> bool:
    return granted == requested or (granted, requested) == ("write", "read")


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    capability: str
    resource_type: str
    resource_scope: str
    action_kind: str
    risk_level: str
    title: str
    summary: str
    resource_display: str | None = None
    use_integration_policy: bool = True
    policy_integration_id: UUID | None = None
    policy_resource_key: str | None = None
    policy_hard_unavailable_capabilities: frozenset[str] = frozenset()
    approval_details: dict[str, Any] | None = None
    display_items: tuple[PermissionDisplayItem, ...] = ()
    sensitive_access: bool = False
    available_decisions: tuple[str, ...] = (
        "allow_once",
        "allow_session",
        "deny",
    )
    # Derived from the current shell inspection, never from approval payloads or grants.
    shell_metadata_paths: frozenset[str] = frozenset()
    session_grants: tuple[SessionGrantScope, ...] = ()
    administrator_paths: tuple[str, ...] = ()
    administrator_analysis_complete: bool = True

    @property
    def grant_capabilities(self) -> tuple[str, ...]:
        grants = self.effective_session_grants
        return (
            tuple(dict.fromkeys(capability for grant in grants for capability in (
                *(f"category.{category}" for category in _CATEGORY_LABELS if category_covers(category, grant.access)),
                grant.capability,
            )))
            if grants else (self.capability,)
        )

    @property
    def effective_session_grants(self) -> tuple[SessionGrantScope, ...]:
        if self.session_grants:
            return self.session_grants
        access = _access_for_capability(self.capability)
        return (
            SessionGrantScope(
                access,
                self.resource_type,
                self.resource_scope,
                self.resource_display,
            ),
        ) if access is not None else ()


def session_grants_payload(request: PermissionRequest) -> list[dict[str, str | None]]:
    return [
        {
            "category": grant.access,
            "resourceType": grant.resource_type,
            "resourceScope": grant.resource_scope,
            "resourceDisplay": grant.resource_display,
        }
        for grant in request.effective_session_grants
    ]


def session_grant_options(request: PermissionRequest) -> dict[str, PermissionRequest]:
    options = {"requested": request}
    grants = request.effective_session_grants
    if request.sensitive_access or "allow_session" not in request.available_decisions or len(grants) != 1:
        return options
    grant = grants[0]
    if grant.access not in {"read", "write", "delete"}:
        return options
    if grant.resource_type in {"session_path", "execution_path"}:
        parsed = (_root_path_scope(grant.resource_scope) if grant.resource_type == "session_path"
                  else _execution_directory_scope(grant.resource_scope))
        if parsed is None:
            return options
        scope = json.loads(grant.resource_scope)
        path = scope["path"]
        parent_path = str(PurePosixPath(path).parent) if grant.resource_type == "session_path" else parent(path)
        if parent_path == path:
            return options
        display = f"{scope['root']}:{parent_path}" if grant.resource_type == "session_path" else parent_path
        broader = replace(grant, resource_scope=json.dumps(
            {**scope, "path": parent_path, "recursive": True}, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
        ), resource_display=display)
        options["parent"] = replace(request, session_grants=(broader,))
    elif grant.resource_type == "shell_action" and request.resource_type == "shell_command":
        scope = _shell_command_scope(request.resource_scope)
        if scope is not None:
            action = json.loads(scope["action"])
            if not action["elevated"] and action["environment"] == hashlib.sha256(b"{}").hexdigest():
                options["computer"] = replace(request, session_grants=(SessionGrantScope(
                    grant.access, "shell_computer", scope["agentId"], "Shell",
                ),))
    return options


def session_grant_options_payload(request: PermissionRequest) -> list[dict[str, Any]]:
    labels = {"requested": "Angefragter Bereich", "parent": "Übergeordneter Ordner", "computer": "Shell auf diesem Computer"}
    return [
        {
            "id": key,
            "label": "Dieser Befehl" if key == "requested" and request.resource_type == "shell_command" else labels[key],
            "display": session_grants_display(option),
            "scopeDisplay": session_grants_scope_display(option),
            "fingerprint": session_grants_fingerprint(
                request.capability, request.resource_type, request.resource_scope, session_grants_payload(option),
            ),
        }
        for key, option in session_grant_options(request).items()
    ]


def session_grants_scope_display(request: PermissionRequest) -> list[str]:
    """Describe grant resources without repeating commands or action categories."""
    collections = {
        "execution_agent_collection": "Deine verfügbaren Computer",
        "session_collection": "Deine Sessions",
        "memory_collection": "Deine Erinnerungen",
        "personal_variable_collection": "Deine Variablen und Secrets",
    }
    result = []
    for grant in request.effective_session_grants:
        if grant.resource_type == "shell_action":
            result.append("Diesen Befehl")
        elif grant.resource_type == "shell_computer":
            result.append("Shell auf diesem Computer")
        elif grant.resource_type in {"execution_path", "session_path", "administrator_path"}:
            scope = json.loads(grant.resource_scope)
            suffix = " einschließlich Unterpfaden" if scope.get("recursive") else " (exakter Pfad)"
            result.append(f'Pfad „{grant.resource_display}“{suffix}')
        elif grant.access == "navigate":
            result.append(f'Ordner „{grant.resource_display}“ und Unterordner (Navigation)')
        elif grant.resource_type == "skill_collection":
            result.append(
                "Verfügbare Skills" if request.capability == "skills.load"
                else "Globale Skills" if grant.resource_scope.startswith("instance:")
                else "Persönliche Skills"
            )
        else:
            result.append(collections.get(grant.resource_type) or grant.resource_display or request.resource_display or "Angezeigte Ressource")
    return result


def session_grants_fingerprint(
    capability: str,
    resource_type: str,
    resource_scope: str,
    scopes: list[dict[str, Any]],
) -> str:
    return hashlib.sha256(json.dumps(
        {
            "capability": capability,
            "resourceScope": resource_scope,
            "resourceType": resource_type,
            "sessionGrants": scopes,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()).hexdigest()


def session_grants_display(request: PermissionRequest) -> list[str]:
    result = []
    for grant in request.effective_session_grants:
        if grant.access == "navigate":
            result.append(f"Navigation im Ordner und seinen Unterordnern: {grant.resource_display}")
            continue
        if grant.resource_type == "shell_action":
            scope = json.loads(grant.resource_scope)
            result.append(f"{_CATEGORY_LABELS[grant.access]} · Diesen Shell-Befehl{' als Administrator' if scope['elevated'] else ''} ausführen: {scope['command']} · Arbeitsverzeichnis: {scope['currentDirectory']}")
            continue
        action = _CATEGORY_LABELS[grant.access]
        if grant.resource_type == "shell_computer":
            result.append(f"{action} · Shell auf diesem Computer")
            continue
        if grant.resource_type in {"execution_path", "session_path"}:
            action += " · einschließlich Unterpfaden" if json.loads(grant.resource_scope).get("recursive") else " · exakter Pfad"
        if grant.resource_type == "administrator_path":
            action += " als Administrator"
        area = grant.resource_display or "Angezeigter Bereich"
        result.append(f"{action}: {area}")
    return result


_INTEGRATION_OPERATION_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_INTEGRATION_RESOURCE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,499}$")
_CONFIDENTIAL_PATH_PARTS = frozenset({".ssh", ".gnupg", ".aws", ".azure", ".kube"})
_CONFIDENTIAL_FILENAMES = frozenset({".env", ".netrc", ".npmrc", ".pypirc"})
_SECRET_NAME = re.compile(
    r"(?:^|[^a-z0-9])(?:password|passwd|passphrase|secrets?|credentials?|api[_-]?key|"
    r"private[_-]?key|(?:access[_-]?|auth[_-]?|refresh[_-]?)?tokens?)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


def confidential_execution_path(path: str) -> bool:
    parsed = PurePosixPath(path)
    return bool(
        _CONFIDENTIAL_PATH_PARTS.intersection(part.casefold() for part in parsed.parts)
        or parsed.name.casefold() in _CONFIDENTIAL_FILENAMES
        or parsed.name.casefold().startswith(".env.")
    )


def _shell_may_expose_secret(
    current_directory: str, inspection: ShellInspectResult,
) -> bool:
    directory = PurePosixPath(current_directory)
    for target in inspection.targets:
        if target.access != "read":
            continue
        path = PurePosixPath(target.path)
        try:
            parts = path.relative_to(directory).parts
        except ValueError:
            parts = path.parts
        if any(_SECRET_NAME.search(part) for part in parts):
            return True

    for command in inspection.commands:
        program, *arguments = command.tokens
        program = PurePosixPath(program).name
        if program == "env" and not any(
            not argument.startswith("-") and "=" not in argument for argument in arguments
        ):
            return True
        if program == "printenv" and (
            not any(not argument.startswith("-") for argument in arguments)
            or any(_SECRET_NAME.search(argument) for argument in arguments)
        ):
            return True
        if program == "set" and not arguments:
            return True
        if program in {"export", "declare"} and "-p" in arguments:
            return True
        if program in {"echo", "printf"} and any(
            "$" in argument and _SECRET_NAME.search(argument) for argument in arguments
        ):
            return True
    return False


def integration_operation_scope(
    integration_id: UUID,
    binding_revision: str,
    operation_key: str,
    *,
    resource_key: str | None = None,
) -> str:
    if len(binding_revision) > 50_000:
        raise ValueError("binding revision is too large")
    try:
        revision_value = json.loads(
            binding_revision,
            parse_constant=lambda _value: None,
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("binding revision must be canonical JSON") from exc
    if not isinstance(revision_value, dict):
        raise ValueError("binding revision must be a canonical object")
    if json.dumps(
        revision_value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ) != binding_revision:
        raise ValueError("binding revision must be canonical JSON")
    if _INTEGRATION_OPERATION_KEY.fullmatch(operation_key) is None:
        raise ValueError("operation key is invalid")
    if resource_key is not None and _INTEGRATION_RESOURCE_KEY.fullmatch(resource_key) is None:
        raise ValueError("resource key is invalid")
    return json.dumps(
        {
            "bindingRevision": hashlib.sha256(binding_revision.encode()).hexdigest(),
            "integrationId": str(integration_id),
            "operationKey": operation_key,
            **({"resourceKey": resource_key} if resource_key is not None else {}),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def integration_resource_scope(
    integration_id: UUID,
    binding_revision: str,
    resource_key: str = "account",
) -> str:
    operation = json.loads(
        integration_operation_scope(
            integration_id,
            binding_revision,
            "resource",
            resource_key=resource_key,
        )
    )
    operation.pop("operationKey")
    return json.dumps(
        operation, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )


def integration_operation_permission(
    *,
    capability: str,
    integration_id: UUID,
    binding_revision: str,
    operation_key: str,
    title: str,
    summary: str,
    resource_key: str | None = None,
    grant_resource_key: str | None = None,
    resource_display: str | None = None,
    use_integration_policy: bool = True,
    action_kind: str = "integration_operation",
    risk_level: str = "medium",
    display_items: tuple[PermissionDisplayItem, ...] = (),
    available_decisions: tuple[str, ...] = (
        "allow_once",
        "allow_session",
        "deny",
    ),
) -> PermissionRequest:
    access = _access_for_capability(capability)
    return PermissionRequest(
        capability=capability,
        resource_type="integration_operation",
        resource_scope=integration_operation_scope(
            integration_id,
            binding_revision,
            operation_key,
            resource_key=resource_key,
        ),
        action_kind=action_kind,
        risk_level=risk_level,
        title=title,
        summary=summary,
        resource_display=resource_display,
        use_integration_policy=use_integration_policy,
        policy_integration_id=integration_id,
        policy_resource_key=resource_key,
        display_items=display_items,
        available_decisions=available_decisions,
        session_grants=(
            SessionGrantScope(
                access,
                "integration_resource",
                integration_resource_scope(
                    integration_id,
                    binding_revision,
                    grant_resource_key or resource_key or "account",
                ),
                resource_display,
            ),
        ) if access is not None else (),
    )


def _integration_operation_scope(resource_scope: str) -> dict[str, str] | None:
    try:
        value = json.loads(resource_scope)
        if not isinstance(value, dict) or set(value) not in (
            {"bindingRevision", "integrationId", "operationKey"},
            {"bindingRevision", "integrationId", "operationKey", "resourceKey"},
        ):
            return None
        integration_id = UUID(value["integrationId"])
        if str(integration_id) != value["integrationId"]:
            return None
        if re.fullmatch(r"[0-9a-f]{64}", value["bindingRevision"]) is None:
            return None
        if _INTEGRATION_OPERATION_KEY.fullmatch(value["operationKey"]) is None:
            return None
        resource_key = value.get("resourceKey")
        if resource_key is not None and _INTEGRATION_RESOURCE_KEY.fullmatch(resource_key) is None:
            return None
        canonical = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return value if canonical == resource_scope else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _integration_resource_scope(resource_scope: str) -> dict[str, str] | None:
    try:
        value = json.loads(resource_scope)
        if not isinstance(value, dict) or set(value) != {"bindingRevision", "integrationId", "resourceKey"}:
            return None
        integration_id = UUID(value["integrationId"])
        if (
            str(integration_id) != value["integrationId"]
            or re.fullmatch(r"[0-9a-f]{64}", value["bindingRevision"]) is None
            or _INTEGRATION_RESOURCE_KEY.fullmatch(value["resourceKey"]) is None
        ):
            return None
        canonical = json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        return value if canonical == resource_scope else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def path_permission(
    *,
    root_files: RootFileService,
    root: FileRoot | str,
    capability: str,
    relative_path: str,
    action_kind: str,
    action_label: str,
) -> PermissionRequest:
    selected_root = FileRoot(root)
    normalized_path = root_files.normalize_relative_path(relative_path)
    root_identity = root_files.root_identity(selected_root)
    scope = json.dumps(
        {
            "identity": root_identity,
            "path": normalized_path,
            "root": selected_root.value,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    access = _access_for_capability(capability)
    return PermissionRequest(
        capability=(
            {"filesystem.read": "session.files.read", "filesystem.delete": "session.files.delete"}.get(capability, capability)
            if selected_root == FileRoot.SESSION else capability
        ),
        resource_type="root_path",
        resource_scope=scope,
        action_kind=action_kind,
        risk_level="high" if capability == "filesystem.delete" else "medium",
        title=f"{action_label} erlauben?",
        summary=(
            f"Der Agent möchte {selected_root.value}:{normalized_path} verwenden."
        ),
        resource_display=f"{selected_root.value}:{normalized_path}",
        session_grants=(
            SessionGrantScope(
                access,
                "session_path",
                json.dumps({**json.loads(scope), "recursive": True}, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                f"{selected_root.value}:{normalized_path}",
            ),
        ) if access is not None else (),
    )


def project_session_path_permission(
    *,
    project_id: UUID,
    session_id: UUID,
    relative_path: str,
    action_kind: str,
    action_label: str,
    capability: str = "session.files.read",
) -> PermissionRequest:
    scope = json.dumps(
        {
            "path": RootFileService(UUID(int=0)).normalize_relative_path(relative_path),
            "projectId": str(project_id),
            "sessionId": str(session_id),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return PermissionRequest(
        capability=capability,
        resource_type="project_session_path",
        resource_scope=scope,
        action_kind=action_kind,
        risk_level="medium",
        title=f"{action_label} erlauben?",
        summary="Der Agent möchte Dateien einer Session im aktuellen Projekt lesen.",
        resource_display=f"Projekt-Session:{relative_path}",
    )


def _project_session_path_scope(
    resource_scope: str,
) -> tuple[UUID, UUID, str] | None:
    try:
        value = json.loads(resource_scope)
        if not isinstance(value, dict) or set(value) != {
            "path",
            "projectId",
            "sessionId",
        }:
            return None
        project_id = UUID(value["projectId"])
        session_id = UUID(value["sessionId"])
        path = RootFileService(UUID(int=0)).normalize_relative_path(value["path"])
        canonical = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if (
            canonical != resource_scope
            or str(project_id) != value["projectId"]
            or str(session_id) != value["sessionId"]
            or path != value["path"]
        ):
            return None
        return project_id, session_id, path
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, RootFileError):
        return None


def execution_directory_scope(agent_id: UUID, path: str, *, recursive: bool = False) -> str:
    absolute_path(path)
    return json.dumps(
        {
            "agentId": str(agent_id),
            "path": path,
            **({"recursive": True} if recursive else {}),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def execution_directory_permission(
    agent_id: UUID,
    path: str,
    *,
    capability: str = "execution.directory",
    action_kind: str = "change_directory",
    title: str = "Verzeichnis verwenden?",
    summary: str | None = None,
) -> PermissionRequest:
    access = _access_for_capability(capability)
    return PermissionRequest(
        capability=capability,
        resource_type="execution_directory",
        resource_scope=execution_directory_scope(agent_id, path),
        action_kind=action_kind,
        risk_level="medium",
        title=title,
        summary=summary or (
            f"Der Agent möchte im Verzeichnis {path} und seinen Unterordnern navigieren. Datei- und Shell-Rechte werden separat geprüft."
            if access == "navigate" else f"Der Agent möchte das Verzeichnis {path} verwenden."
        ),
        resource_display=path,
        use_integration_policy=False,
        sensitive_access=capability != "file.provide" and confidential_execution_path(path),
        display_items=(PermissionDisplayItem("Computer", str(agent_id)),),
        available_decisions=(
            ("allow_once", "allow_session", "deny")
            if access is not None
            else ("allow_once", "deny")
        ),
        session_grants=(
            SessionGrantScope(
                access,
                "execution_directory" if access == "navigate" else "execution_path",
                execution_directory_scope(agent_id, path, recursive=access != "navigate"),
                path,
            ),
        ) if access is not None else (),
    )


def system_info_permission(agent_id: UUID) -> PermissionRequest:
    return PermissionRequest(
        capability="system.info",
        resource_type="execution_agent",
        resource_scope=str(agent_id),
        action_kind="get_system_info",
        risk_level="low",
        title="Systeminformationen lesen?",
        summary="Der Agent möchte grundlegende Informationen des ausgewählten Computers lesen.",
        resource_display="Ausgewählter Computer",
    )


def execution_agents_list_permission(user_id: UUID) -> PermissionRequest:
    return PermissionRequest(
        capability="execution_agent.list", resource_type="execution_agent_collection",
        resource_scope=str(user_id), action_kind="list_execution_agents", risk_level="low",
        title="Computer anzeigen?", summary="Der Agent möchte Ihre verfügbaren Computer anzeigen.",
    )


def execution_agent_select_permission(agent_id: UUID, alias: str) -> PermissionRequest:
    return PermissionRequest(
        capability="execution_agent.select", resource_type="execution_agent",
        resource_scope=str(agent_id), action_kind="select_execution_agent", risk_level="medium",
        title="Computer übernehmen?",
        summary=f"Der Agent möchte für diese Session den Computer {alias} übernehmen.",
        resource_display=alias,
    )


def session_permission(target_session_id: UUID, *, private_access: bool = False) -> PermissionRequest:
    return PermissionRequest(
        capability="session.read_sensitive" if private_access else "session.read",
        resource_type="session",
        resource_scope=str(target_session_id),
        action_kind="session_read",
        risk_level="medium",
        title="Private Session lesen?" if private_access else "Andere Session lesen?",
        summary="Der Agent möchte den Gesprächsverlauf einer privaten Session lesen." if private_access else "Der Agent möchte den Gesprächsverlauf einer anderen Session lesen.",
    )


def session_search_permission(*, include_private: bool = False, trust_level: TrustLevel = TrustLevel.PRIVATE) -> PermissionRequest:
    private_access = include_private and trust_level == TrustLevel.PUBLIC
    return PermissionRequest(
        capability="session.read_sensitive" if private_access else "session.search",
        resource_type="session_collection",
        resource_scope="all" if include_private or trust_level == TrustLevel.PRIVATE else "public",
        action_kind="session_search",
        risk_level="medium",
        title="Sessions durchsuchen?",
        summary="Der Agent möchte Titel und Inhalte anderer Sessions einschließlich privater Sessions durchsuchen." if private_access else "Der Agent möchte Titel und Inhalte anderer Sessions durchsuchen.",
    )


def session_browse_permission(*, include_private: bool = False, trust_level: TrustLevel = TrustLevel.PRIVATE) -> PermissionRequest:
    return replace(
        session_search_permission(include_private=include_private, trust_level=trust_level),
        action_kind="session_browse", title="Sessions anzeigen?",
    )


def automation_collection_scope(user_id: UUID, session_id: UUID) -> str:
    return json.dumps(
        {"sessionId": str(session_id), "userId": str(user_id)},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def automation_scope(
    automation_id: UUID, revision: int, user_id: UUID, session_id: UUID
) -> str:
    if revision < 1:
        raise ValueError("automation revision must be positive")
    return json.dumps(
        {
            "automationId": str(automation_id),
            "revision": revision,
            "sessionId": str(session_id),
            "userId": str(user_id),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _automation_collection_scope(resource_scope: str) -> tuple[UUID, UUID] | None:
    try:
        value = json.loads(resource_scope)
        if set(value) != {"sessionId", "userId"}:
            return None
        session_id = UUID(value["sessionId"])
        user_id = UUID(value["userId"])
        canonical = automation_collection_scope(user_id, session_id)
        return (user_id, session_id) if canonical == resource_scope else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _automation_scope(
    resource_scope: str,
) -> tuple[UUID, int, UUID, UUID] | None:
    try:
        value = json.loads(resource_scope)
        if set(value) != {"automationId", "revision", "sessionId", "userId"}:
            return None
        automation_id = UUID(value["automationId"])
        revision = value["revision"]
        session_id = UUID(value["sessionId"])
        user_id = UUID(value["userId"])
        if isinstance(revision, bool) or not isinstance(revision, int):
            return None
        canonical = automation_scope(automation_id, revision, user_id, session_id)
        return (
            (automation_id, revision, user_id, session_id)
            if canonical == resource_scope
            else None
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def memory_write_permission(
    session_id: UUID, action: str, *, pinned: bool = False
) -> PermissionRequest:
    updating = action == "update"
    forgetting = action == "forget"
    return PermissionRequest(
        capability="memory.write",
        resource_type="memory_collection" if forgetting else "session",
        resource_scope=f"forget:{session_id}" if forgetting else str(session_id),
        action_kind="memory_write",
        risk_level="high" if forgetting else "medium",
        title=(
            "Memory vergessen?"
            if forgetting
            else "Memory aktualisieren?"
            if updating
            else "Memory speichern?"
        ),
        summary=(
            "Der Agent möchte ein bestehendes Memory endgültig löschen."
            if forgetting
            else "Der Agent möchte ein bestehendes Memory aktualisieren."
            if updating
            else "Der Agent möchte ein neues Memory speichern."
        ),
        resource_display="Persönliche Memories" if forgetting else "Aktuelle Session",
        display_items=(
            (PermissionDisplayItem("Berücksichtigung", "Immer"),) if pinned else ()
        ),
    )


def webfetch_permission(url: str) -> PermissionRequest:
    canonical_url = canonicalize_url(url)
    return PermissionRequest(
        capability="network.fetch",
        resource_type="url",
        resource_scope=hashlib.sha256(canonical_url.encode("utf-8")).hexdigest(),
        action_kind="webfetch",
        risk_level="medium",
        title="Webseite abrufen?",
        summary=f"Der Agent möchte {canonical_url} abrufen.",
        resource_display=canonical_url,
    )


def transcript_permission(url: str) -> PermissionRequest:
    canonical_url = canonicalize_youtube_video_url(url)
    return PermissionRequest(
        capability="network.fetch",
        resource_type="url",
        resource_scope=hashlib.sha256(canonical_url.encode("utf-8")).hexdigest(),
        action_kind="fetch_transcript",
        risk_level="medium",
        title="Untertitel abrufen?",
        summary=f"Der Agent möchte veröffentlichte Untertitel von {canonical_url} abrufen.",
        resource_display=canonical_url,
    )


def web_search_permission() -> PermissionRequest:
    return PermissionRequest(
        capability="network.search",
        resource_type="network_service",
        resource_scope="web_search",
        action_kind="web_search",
        risk_level="medium",
        title="Websuche ausführen?",
        summary="Der Agent möchte einen konfigurierten Websuchdienst verwenden.",
        resource_display="Konfigurierte Websuche",
    )


def memory_read_permission(user_id: UUID) -> PermissionRequest:
    return PermissionRequest(
        capability="memory.read",
        resource_type="memory_collection",
        resource_scope=str(user_id),
        action_kind="memory_retrieve",
        risk_level="medium",
        title="Memories lesen?",
        summary="Der Agent möchte gespeicherte Memories lesen.",
        resource_display="Eigene Memories",
    )


def media_permission(
    session_id: UUID,
    capability: str,
    tool_name: str,
    *,
    provider_id: UUID | None = None,
    model_id: UUID | None = None,
    model_name: str | None = None,
    trust_level: str | None = None,
    source_attachment_ids: tuple[UUID, ...] = (),
    source_files: tuple[dict[str, str], ...] = (),
) -> PermissionRequest:
    labels = {
        "media.generate.image": ("Bild erzeugen?", "ein Bild erzeugen"),
        "media.edit.image": ("Bild bearbeiten?", "Bilder bearbeiten"),
        "media.generate.speech": ("Sprache erzeugen?", "eine Sprachaufnahme erzeugen"),
        "media.generate.audio": ("Audio erzeugen?", "eine Audiodatei erzeugen"),
        "media.transcribe.audio": ("Audio transkribieren?", "eine Audiodatei transkribieren"),
        "media.analyze.video": ("Video analysieren?", "ein Video analysieren"),
    }
    title, action = labels.get(capability, ("Medienaktion erlauben?", "Medien verarbeiten"))
    targeted = provider_id is not None and model_id is not None
    return PermissionRequest(
        capability=capability,
        resource_type="media_model" if targeted else "session",
        resource_scope=(
            json.dumps(
                {
                    "sessionId": str(session_id),
                    "providerId": str(provider_id),
                    "modelId": str(model_id),
                    "trustLevel": trust_level,
                    **(
                        {
                            "sourceAttachmentIds": sorted(
                                str(attachment_id)
                                for attachment_id in source_attachment_ids
                            )
                        }
                        if source_attachment_ids
                        else {}
                    ),
                    **({"sourceFiles": list(source_files)} if source_files else {}),
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            if targeted
            else str(session_id)
        ),
        action_kind=tool_name,
        risk_level="medium",
        title=title,
        summary=(
            f"Der Agent möchte mit {model_name} {action}."
            if targeted and model_name
            else f"Der Agent möchte in der aktuellen Session {action}."
        ),
        resource_display=model_name if targeted else None,
    )


def agent_identity_permission(
    user_id: UUID,
    *,
    changes_name: bool,
    changes_instructions: bool,
    persona_name: str | None = None,
    set_as_default: bool | None = None,
) -> PermissionRequest:
    if changes_name and changes_instructions:
        summary = f"Der Agent möchte seinen Namen und die Persona „{persona_name}“ ändern."
    elif changes_name:
        summary = "Der Agent möchte seinen Namen ändern."
    elif changes_instructions:
        summary = f"Der Agent möchte die Persona „{persona_name}“ ändern."
    else:
        summary = ""
    if set_as_default is not None:
        default_summary = (
            f"Die Persona „{persona_name}“ soll als persönlicher Standard verwendet werden."
            if set_as_default else "Der persönliche Persona-Standard soll aufgehoben werden."
        )
        summary = f"{summary} {default_summary}".strip()
    return PermissionRequest(
        capability="settings.agent_identity.update",
        resource_type="agent_identity",
        resource_scope=str(user_id),
        action_kind="update_agent_identity",
        risk_level="medium",
        title="Agent-Persona ändern?",
        summary=summary,
        resource_display="Eigene Agent-Persona",
    )


def user_profile_permission(
    user_id: UUID, *, field_labels: list[str], model_scope: str
) -> PermissionRequest:
    scopes = {
        "private_models": "private Modelle",
        "public_models": "öffentliche Modelle",
        "both_models": "private und öffentliche Modelle",
    }
    fields = ", ".join(field_labels)
    return PermissionRequest(
        capability="settings.user_profile.update",
        resource_type="user_profile",
        resource_scope=json.dumps(
            {"modelScope": model_scope, "userId": str(user_id)},
            separators=(",", ":"), sort_keys=True,
        ),
        action_kind="update_user_profile",
        risk_level="medium",
        title="Persönliches Profil ändern?",
        summary=(
            f"Der Agent möchte {fields} im persönlichen Profil für "
            f"{scopes[model_scope]} ändern."
        ),
        resource_display=f"Eigenes persönliches Profil ({scopes[model_scope]})",
    )


def personal_settings_permission(
    user_id: UUID, area: str, action: str, *, fields: tuple[str, ...] = (),
    integration_id: UUID | None = None, changes: dict[str, Any] | None = None,
) -> PermissionRequest:
    verbs = {"get": "lesen", "create": "anlegen", "update": "ändern", "delete": "entfernen"}
    labels = {"memory": "Memory", "user_profile": "persönliches Profil", "agent_identity": "Agent-Persona", "integrations": "Integrationen", "variables": "persönliche Variablen"}
    return PermissionRequest(
        capability="settings.personal.read" if action == "get" else "settings.personal.update",
        resource_type="personal_settings",
        resource_scope=json.dumps(
            {"userId": str(user_id), "area": area, "integrationId": str(integration_id) if integration_id else None},
            separators=(",", ":"), sort_keys=True,
        ),
        action_kind=f"personal_settings_{action}",
        risk_level="low" if action == "get" else "medium",
        title="Persönliche Einstellungen lesen?" if action == "get" else "Persönliche Einstellungen ändern?",
        summary=f"Der Agent möchte {labels[area]} {verbs[action]}" + (f" ({', '.join(fields)})" if fields else "") + ".",
        resource_display=labels[area],
        display_items=(PermissionDisplayItem("Änderungen", tuple(
            f"{field}: {'wird geändert' if field == 'bearer_token_ref' else str(value)[:200]}"
            for field, value in sorted(changes.items())
        ), "list"),) if changes else (
            (PermissionDisplayItem("Felder", fields, "list"),) if fields else ()
        ),
        use_integration_policy=False,
    )


def personal_variables_permission(
    user_id: UUID,
    *,
    write: bool = False,
    names: tuple[str, ...] = (),
) -> PermissionRequest:
    return PermissionRequest(
        capability="secrets.write" if write else "secrets.access",
        resource_type="personal_variable_collection",
        resource_scope=str(user_id),
        resource_display="Eigene Variablen und Secrets",
        action_kind="personal_variable_write" if write else "personal_variable_access",
        risk_level="high" if write else "medium",
        title="Variable speichern erlauben?" if write else "Variablenzugriff erlauben?",
        summary=(
            "Der Agent möchte eine persönliche Variable speichern."
            if write
            else "Der Agent möchte persönliche Variablen für diese Aktion verwenden."
        ),
        display_items=(
            (PermissionDisplayItem("Variablen", names, "list"),) if names else ()
        ),
        use_integration_policy=False,
    )


def _user_profile_scope(resource_scope: str) -> tuple[UUID, str] | None:
    try:
        value = json.loads(resource_scope)
        if not isinstance(value, dict) or set(value) != {"modelScope", "userId"}:
            return None
        if not isinstance(value["userId"], str) or value["modelScope"] not in (
            "private_models", "public_models", "both_models"
        ):
            return None
        user_id = UUID(value["userId"])
        if str(user_id) != value["userId"] or json.dumps(
            value, separators=(",", ":"), sort_keys=True
        ) != resource_scope:
            return None
        return user_id, value["modelScope"]
    except (TypeError, ValueError):
        return None


def skill_load_permission(user_id: UUID, skill_name: str) -> PermissionRequest:
    return PermissionRequest(
        capability="skills.load",
        resource_type="skill_collection",
        resource_scope=str(user_id),
        action_kind="load_skill",
        risk_level="low",
        title="Skill laden?",
        summary=f'Der Agent möchte den Skill „{skill_name}“ verwenden.',
        resource_display=skill_name,
    )


def skill_write_permission(
    user_id: UUID,
    skill_name: str,
    *,
    approval_details: dict[str, Any] | None = None,
    scope: str = "personal",
) -> PermissionRequest:
    return PermissionRequest(
        capability="skills.write",
        resource_type="skill_collection",
        resource_scope=str(user_id) if scope == "personal" else f"instance:{user_id}",
        action_kind="build_skill",
        risk_level="medium",
        title="Skill speichern?",
        summary=f'Der Agent möchte den {"globalen" if scope == "instance" else "persönlichen"} Skill „{skill_name}“ speichern.',
        resource_display=skill_name,
        approval_details=approval_details,
    )


def skill_edit_permission(
    skill_id: UUID,
    skill_name: str,
    *,
    approval_details: dict[str, Any] | None = None,
    scope: str = "personal",
) -> PermissionRequest:
    return PermissionRequest(
        capability="skills.write",
        resource_type="skill",
        resource_scope=str(skill_id) if scope == "personal" else f"instance:{skill_id}",
        action_kind="edit_skill",
        risk_level="medium",
        title="Skill ändern?",
        summary=f'Der Agent möchte den {"globalen" if scope == "instance" else "persönlichen"} Skill „{skill_name}“ ändern.',
        resource_display=skill_name,
        approval_details=approval_details,
    )


def _root_path_scope(resource_scope: str) -> tuple[FileRoot, str, PurePosixPath] | None:
    try:
        value = json.loads(resource_scope)
        if not isinstance(value, dict) or set(value) not in (
            {"identity", "path", "root"}, {"identity", "path", "root", "recursive"},
        ) or "recursive" in value and value["recursive"] is not True:
            return None
        root = FileRoot(value["root"])
        if root is not FileRoot.SESSION:
            return None
        identity = value["identity"]
        path = value["path"]
        if not isinstance(identity, str) or not isinstance(path, str):
            return None
        parsed = PurePosixPath(path)
        if path != "." and (
            parsed.is_absolute()
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            return None
        if json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ) != resource_scope:
            return None
        return root, identity, parsed
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _execution_directory_scope(resource_scope: str) -> tuple[UUID, str] | None:
    try:
        value = json.loads(resource_scope)
        if not isinstance(value, dict) or set(value) not in (
            {"agentId", "path"}, {"agentId", "path", "recursive"},
        ) or "recursive" in value and value["recursive"] is not True:
            return None
        agent_id = UUID(value["agentId"])
        path = value["path"]
        canonical = execution_directory_scope(agent_id, path, recursive=value.get("recursive", False))
        return (
            agent_id,
            path,
        ) if canonical == resource_scope else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _path_is_within(resource: str | PurePosixPath, scope: str | PurePosixPath) -> bool:
    if str(scope) == ".":
        return True
    if not is_absolute_path(str(resource)):
        return PurePosixPath(scope) in (PurePosixPath(resource), *PurePosixPath(resource).parents)
    return within(str(resource), str(scope))


def _path_key(path: str) -> str:
    return path if path.startswith("/") else path.casefold()


def shell_command_scope(
    agent_id: UUID,
    current_directory: str,
    classification: Literal["read", "write"],
    prefixes: list[list[str]],
    paths: list[str],
    analysis_complete: bool,
    target_accesses: list[Literal["read", "write", "exempt"] | None] | None = None,
    action: str | None = None,
    required_categories: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "agentId": str(agent_id),
            "analysisComplete": analysis_complete,
            "classification": classification,
            "currentDirectory": current_directory,
            "paths": paths,
            "prefixes": prefixes,
            "targetAccesses": target_accesses if target_accesses is not None else [None] * len(paths),
            **({"action": action} if action is not None else {}),
            **({"requiredCategories": required_categories} if required_categories is not None else {}),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def shell_action_scope(
    agent_id: UUID, current_directory: str, command: str, *, elevated: bool = False,
    environment: dict | None = None,
) -> str:
    """Bind consent to a command and execution context, without storing environment values."""
    return json.dumps({
        "agentId": str(agent_id), "currentDirectory": current_directory,
        "command": command, "elevated": elevated,
        "environment": hashlib.sha256(json.dumps(
            environment or {}, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest(),
    }, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def shell_command_permissions(
    agent_id: UUID, current_directory: str, command_line: str, inspection: ShellInspectResult,
    *, environment: dict | None = None,
) -> tuple[PermissionRequest, ...]:
    prefixes = (
        [command.prefix for command in inspection.commands if command.prefix is not None]
        if inspection.commands
        and all(command.prefix is not None for command in inspection.commands)
        else []
    )
    metadata_kinds = {"toolchain_metadata", "discard"}
    metadata_paths = frozenset(
        target.path for target in inspection.targets if target.kind in metadata_kinds
    ) - {
        target.path for target in inspection.targets if target.kind not in metadata_kinds
    }
    action = shell_action_scope(
        agent_id, current_directory, command_line, environment=environment,
    )
    scope = shell_command_scope(
        agent_id, current_directory, inspection.classification, prefixes,
        [target.path for target in inspection.targets], inspection.analysis_complete,
        ["exempt" if target.kind in metadata_kinds else target.access for target in inspection.targets],
        action, inspection.required_categories,
    )
    confidential_paths = tuple(dict.fromkeys(
        path
        for path in [current_directory, *(target.path for target in inspection.targets)]
        if confidential_execution_path(path)
    ))
    may_expose_secret = _shell_may_expose_secret(current_directory, inspection)
    display_items = (
        PermissionDisplayItem("Shell-Befehl", command_line, "code"),
        PermissionDisplayItem("Computer", str(agent_id)),
        PermissionDisplayItem("Arbeitsverzeichnis", current_directory, "code"),
    )
    if confidential_paths:
        display_items += (PermissionDisplayItem("Besonders geschützte Pfade", confidential_paths, "list"),)
    if may_expose_secret:
        display_items += (PermissionDisplayItem(
            "Mögliche Secret-Ausgabe",
            "Der Befehl könnte Zugangsdaten ausgeben oder an einen Prozess übergeben. Die Prüfung kann sichtbare "
            "Ausgabe und eine direkte Authentifizierungsübergabe nicht sicher unterscheiden. Prüfe das Ausgabeziel.",
        ),)
    request = PermissionRequest(
        capability=f"shell.{inspection.classification}",
        resource_type="shell_command",
        resource_scope=scope,
        action_kind="shell_run",
        risk_level="low" if inspection.classification == "read" else "high",
        title=(
            "Shell-Befehl ohne erkannte Schreibaktion ausführen?"
            if inspection.classification == "read"
            else "Als schreibend eingestuften Shell-Befehl ausführen?"
        ),
        summary=(
            "Es wurden keine bekannten Schreibaktionen erkannt. Der Befehl kann dennoch Nebenwirkungen haben."
            if inspection.classification == "read"
            else "Der Befehl wurde als schreibend eingestuft und kann Dateien verändern, Programme starten und Netzwerkdienste verwenden."
        ) + (
            " Die Analyse ist unvollständig; nicht alle Befehle und Pfade konnten geprüft werden."
            if not inspection.analysis_complete else ""
        ) + (
            " Die Richtlinie für vertrauliche Daten gilt zusätzlich für die geschützten Pfade."
            if confidential_paths else ""
        ) + (
            " Die Richtlinie für vertrauliche Daten gilt zusätzlich für die mögliche Secret-Ausgabe."
            if may_expose_secret else ""
        ),
        resource_display=current_directory,
        shell_metadata_paths=metadata_paths,
        approval_details={
            "shellClassification": inspection.classification,
            "shellCategories": inspection.required_categories,
            "shellAnalysisComplete": inspection.analysis_complete,
            "shellPrefixes": prefixes,
        },
        display_items=display_items,
        sensitive_access=bool(confidential_paths) or may_expose_secret,
    )
    return tuple(replace(
        request, capability=f"shell.{category}",
        title=f"Shell: {_CATEGORY_LABELS[category]} erlauben?",
        risk_level="low" if category == "read" else "high",
        summary=("Der Befehl enthält erkannte Löschaktionen. " if category == "delete" else "") + request.summary,
        session_grants=(SessionGrantScope(category, "shell_action", action, command_line),),
    ) for category in sorted(inspection.required_categories, key=lambda item: item == "read"))


def administrator_permission(
    agent_id: UUID, current_directory: str, *, capability: str,
    command: str | None = None, path: str | None = None,
    inspection: ShellInspectResult | None = None,
    environment: dict | None = None,
) -> PermissionRequest:
    action = shell_action_scope(
        agent_id, current_directory, command, elevated=True, environment=environment,
    ) if command is not None else None
    scope = json.dumps(
        {"agentId": str(agent_id), "currentDirectory": current_directory,
         **({"command": command, "action": action} if command is not None else {"path": path}),
         **({"requiredCategories": inspection.required_categories} if command is not None and inspection else {})},
        sort_keys=True, separators=(",", ":"),
    )
    paths = tuple(target.path for target in inspection.targets) if inspection else (path,)
    confidential = any(confidential_execution_path(value) for value in paths if value)
    secret_output = inspection is not None and _shell_may_expose_secret(current_directory, inspection)
    return PermissionRequest(
        capability=capability, resource_type="execution_administrator",
        resource_scope=scope,
        action_kind="shell_run" if command is not None else "read" if capability == "filesystem.read" else "edit_file",
        risk_level="high", title="Administratorzugriff erlauben?",
        summary="Die angezeigte Aktion wird mit Administratorrechten auf dem ausgewählten Computer ausgeführt.",
        resource_display=path or current_directory,
        display_items=(PermissionDisplayItem("Computer", str(agent_id)),
                       PermissionDisplayItem("Shell-Befehl" if command is not None else "Datei", command or path, "code")),
        approval_details={
            "administratorAgentId": str(agent_id),
            **({"shellClassification": inspection.classification,
                "shellCategories": inspection.required_categories,
                "shellAnalysisComplete": inspection.analysis_complete} if inspection else {}),
        },
        administrator_paths=tuple(value for value in paths if value),
        administrator_analysis_complete=inspection.analysis_complete if inspection else True,
        sensitive_access=confidential or secret_output,
        session_grants=(SessionGrantScope(
            _access_for_capability(capability),
            "shell_action" if action else "administrator_path",
            action or execution_directory_scope(agent_id, path), command if action else path,
        ),),
    )


def _shell_command_scope(resource_scope: str) -> dict[str, Any] | None:
    try:
        value = json.loads(resource_scope)
        if set(value) - {"action", "requiredCategories"} != {
            "agentId",
            "analysisComplete",
            "classification",
            "currentDirectory",
            "paths",
            "prefixes",
            "targetAccesses",
        }:
            return None
        agent_id = UUID(value["agentId"])
        current_directory = value["currentDirectory"]
        classification = value["classification"]
        prefixes = value["prefixes"]
        paths = value["paths"]
        target_accesses = value["targetAccesses"]
        analysis_complete = value["analysisComplete"]
        categories = value.get("requiredCategories")
        if categories is not None and (
            not isinstance(categories, list) or not 1 <= len(categories) <= 3
            or any(category not in ("read", "write", "delete") for category in categories)
            or len(set(categories)) != len(categories)
            or bool(set(categories) & {"write", "delete"}) != (classification == "write")
        ):
            return None
        if (
            not isinstance(current_directory, str)
            or not is_absolute_path(current_directory)
            or classification not in {"read", "write"}
            or not isinstance(analysis_complete, bool)
            or not isinstance(prefixes, list)
            or len(prefixes) > 64
            or any(
                not isinstance(prefix, list)
                or not 1 <= len(prefix) <= 8
                or any(
                    not isinstance(token, str)
                    or not token
                    or len(token) > 256
                    or any(
                        ord(character) < 32 or 127 <= ord(character) <= 159
                        for character in token
                    )
                    for token in prefix
                )
                for prefix in prefixes
            )
            or not isinstance(paths, list)
            or len(paths) > 256
            or not isinstance(target_accesses, list)
            or len(target_accesses) != len(paths)
            or any(access not in {None, "read", "write", "exempt"} for access in target_accesses)
            or any(
                not isinstance(path, str)
                or not is_absolute_path(path)
                for path in paths
            )
        ):
            return None
        canonical = shell_command_scope(
            agent_id,
            current_directory,
            classification,
            prefixes,
            paths,
            analysis_complete,
            target_accesses,
            value.get("action"),
            categories,
        )
        return value if canonical == resource_scope else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def shell_prefix_allowed(settings: ToolSettings, request: PermissionRequest) -> bool:
    scope = _shell_command_scope(request.resource_scope)
    if scope is None:
        return False
    prefixes = scope["prefixes"]
    category = request.capability.removeprefix("shell.")
    if category not in {"read", "write"}:
        return False
    allowed = getattr(settings.shell_prefixes, category)
    return bool(prefixes) and all(
        any(prefix[: len(candidate)] == candidate for candidate in allowed)
        for prefix in prefixes
    )


def session_grant_matches_request(
    capability: str,
    resource_type: str,
    resource_scope: str,
    grant: SessionGrantScope,
) -> bool:
    access = _access_for_capability(capability)
    if resource_type == "shell_command" and grant.resource_type == "shell_computer":
        scope = _shell_command_scope(resource_scope)
        if scope is None or grant.access != access or capability != f"shell.{grant.access}" or grant.access not in scope.get("requiredCategories", []):
            return False
        try:
            action = json.loads(scope["action"])
            if action["elevated"] or action["environment"] != hashlib.sha256(b"{}").hexdigest():
                return False
        except (KeyError, TypeError, ValueError):
            return False
        return grant.resource_scope == scope["agentId"]
    if grant.access != access:
        return False
    if grant.access == "navigate":
        return (
            resource_type == "execution_directory"
            and grant.resource_type == "execution_directory"
            and _execution_directory_scope(resource_scope) is not None
            and grant.resource_scope == resource_scope
        )
    if resource_type in {"execution_administrator", "shell_command"}:
        try:
            scope = json.loads(resource_scope)
            if capability in {"shell.read", "shell.write", "shell.delete"}:
                return grant.resource_type == "shell_action" and grant.resource_scope == scope.get("action")
            return (
                capability in {"filesystem.read", "filesystem.write"}
                and grant.resource_type == "administrator_path"
                and grant.resource_scope == execution_directory_scope(UUID(scope["agentId"]), scope["path"])
            )
        except (ValueError, KeyError, TypeError):
            return False
    if resource_type == "root_path":
        requested = _root_path_scope(resource_scope)
        granted = _root_path_scope(grant.resource_scope)
        return bool(
            grant.resource_type == "session_path"
            and requested
            and granted
            and requested[:2] == granted[:2]
            and requested[2] == granted[2]
        )
    if resource_type == "execution_directory":
        requested = _execution_directory_scope(resource_scope)
        granted = _execution_directory_scope(grant.resource_scope)
        return bool(
            grant.resource_type == "execution_path"
            and requested
            and granted
            and requested[0] == granted[0]
            and _path_key(requested[1]) == _path_key(granted[1])
        )
    if resource_type == "integration_operation":
        requested = _integration_operation_scope(resource_scope)
        granted = _integration_resource_scope(grant.resource_scope)
        return bool(
            grant.resource_type == "integration_resource"
            and requested
            and granted
            and requested["integrationId"] == granted["integrationId"]
            and requested["bindingRevision"] == granted["bindingRevision"]
        )
    if resource_type == "project":
        try:
            project_id = UUID(resource_scope)
            return grant.resource_type == "project" and grant.resource_scope == str(project_id)
        except ValueError:
            return False
    return grant.resource_type == resource_type and grant.resource_scope == resource_scope


def temporary_file_grants(session: ChatSession, agent_id: UUID, base: str, *, writable: bool) -> tuple[PermissionGrant, ...]:
    """Derive scratch rights without persisting another permission source."""
    path = session_temporary_directory(base, session.id)
    return tuple(PermissionGrant(
        capability=f"category.{category}", resource_type="execution_path",
        resource_scope=execution_directory_scope(agent_id, path, recursive=True),
    ) for category in (("read", "write") if writable else ("read",)))


class PermissionPolicy:
    @staticmethod
    async def evaluate(
        db: AsyncSession,
        chat_session: ChatSession,
        request: PermissionRequest,
        root_files: RootFileService,
        policy: ToolExecutionPolicy | None = None,
        *,
        external_working_directory_policy: ToolExecutionPolicy = ToolExecutionPolicy.ASK,
        sensitive_data_policy: ToolExecutionPolicy = ToolExecutionPolicy.ASK,
        agent_profile_id: UUID | None = None,
        trust_level: TrustLevel | None = None,
        permission_session_id: UUID | None = None,
        run_id: UUID | None = None,
        session_write_paths: list[str] | None = None,
        temporary_files_enabled: bool = False,
    ) -> PermissionDecision:
        from mato_loop_backend.permissions.requests import prepare_file_grants

        grant_session_id = permission_session_id or chat_session.id
        if grant_session_id != chat_session.id:
            if chat_session.parent_session_id != grant_session_id:
                return PermissionDecision.DENY
            parent = await db.get(ChatSession, grant_session_id, populate_existing=True)
            if parent is None or parent.user_id != chat_session.user_id:
                return PermissionDecision.DENY

        def grant_binding_conditions() -> tuple[Any, ...]:
            lifetime = PermissionGrant.run_id.is_(None) | (PermissionGrant.run_id == run_id)
            if trust_level is None:
                return (lifetime,)
            profile_condition = (
                PermissionGrant.agent_profile_id.is_(None)
                if agent_profile_id is None
                else PermissionGrant.agent_profile_id == agent_profile_id
            )
            return (lifetime, profile_condition, PermissionGrant.trust_level == trust_level.value)

        def sensitive_decision(sensitive: bool) -> PermissionDecision | None:
            if sensitive and policy == ToolExecutionPolicy.DISABLED:
                return PermissionDecision.DENY
            if not sensitive or sensitive_data_policy == ToolExecutionPolicy.AUTO:
                return None
            return PermissionDecision.DENY if sensitive_data_policy == ToolExecutionPolicy.DISABLED else PermissionDecision.ASK

        if request.resource_type == "user_profile":
            scope = _user_profile_scope(request.resource_scope)
            if (
                scope is None
                or scope[0] != chat_session.user_id
                or request.capability != "settings.user_profile.update"
            ):
                return PermissionDecision.DENY

        if request.resource_type == "personal_settings":
            try:
                scope = json.loads(request.resource_scope)
                owner_id = UUID(scope["userId"])
                integration_id = scope["integrationId"]
                if (
                    set(scope) != {"userId", "area", "integrationId"}
                    or str(owner_id) != scope["userId"]
                    or owner_id != chat_session.user_id
                    or scope["area"] not in {"memory", "user_profile", "agent_identity", "integrations", "variables"}
                    or (integration_id is not None and (scope["area"] != "integrations" or str(UUID(integration_id)) != integration_id))
                    or json.dumps(scope, separators=(",", ":"), sort_keys=True) != request.resource_scope
                    or request.capability not in {"settings.personal.read", "settings.personal.update"}
                ):
                    return PermissionDecision.DENY
            except (ValueError, KeyError, TypeError):
                return PermissionDecision.DENY

        if request.resource_type == "personal_variable_collection":
            try:
                owner_id = UUID(request.resource_scope)
            except ValueError:
                return PermissionDecision.DENY
            if owner_id != chat_session.user_id or request.capability not in {
                "secrets.access",
                "secrets.write",
            }:
                return PermissionDecision.DENY

        if request.resource_type in {"automation_collection", "session_automation_collection", "automation"}:
            collection = request.resource_type != "automation"
            scope = (
                _automation_collection_scope(request.resource_scope)
                if collection
                else _automation_scope(request.resource_scope)
            )
            user_id, session_id = (
                scope if collection else scope[2:]
            ) if scope is not None else (None, None)
            valid_capability = request.capability in (
                {"automation.read", "automation.create"}
                if collection
                else {
                    "automation.read",
                    "automation.update",
                    "automation.run",
                    "automation.delete",
                }
            )
            if (
                not valid_capability
                or user_id != chat_session.user_id
                or session_id != chat_session.id
            ):
                return PermissionDecision.DENY
            if not collection:
                automation = await db.get(Automation, scope[0], populate_existing=True)
                if automation is None or automation.user_id != user_id or automation.revision != scope[1]:
                    return PermissionDecision.DENY
                if automation.execution_session_id == chat_session.id and request.capability in {
                    "automation.read", "automation.update",
                } and policy != ToolExecutionPolicy.DISABLED:
                    policy = ToolExecutionPolicy.AUTO
            elif request.resource_type == "session_automation_collection":
                if request.capability != "automation.read":
                    return PermissionDecision.DENY
                if policy != ToolExecutionPolicy.DISABLED:
                    policy = ToolExecutionPolicy.AUTO

        if request.resource_type == "session" and request.capability in {"session.read", "session.read_sensitive"}:
            try:
                target_id = UUID(request.resource_scope)
                target = (
                    chat_session
                    if target_id == chat_session.id
                    else await db.get(ChatSession, target_id, populate_existing=True)
                )
                if target is None or target.user_id != chat_session.user_id or (
                    target is not chat_session
                    and target.parent_session_id is not None
                    and target.parent_session_id != chat_session.id
                ):
                    return PermissionDecision.DENY
                if (
                    target_id != chat_session.id and trust_level == TrustLevel.PUBLIC
                    and target.trust_level != TrustLevel.PUBLIC.value
                    and request.capability != "session.read_sensitive"
                ):
                    return PermissionDecision.DENY
                if target is chat_session or (
                    target.parent_session_id == chat_session.id
                    or (
                        chat_session.project_id is not None
                        and target.project_id == chat_session.project_id
                        and target.session_type == "chat"
                    )
                ):
                    if policy == ToolExecutionPolicy.DISABLED:
                        return PermissionDecision.DENY
                    if policy == ToolExecutionPolicy.AUTO:
                        return PermissionDecision.ALLOW
            except ValueError:
                return PermissionDecision.DENY

        if request.resource_type == "session_collection":
            if request.resource_scope not in {"all", "public"} or (
                trust_level == TrustLevel.PUBLIC and request.resource_scope == "all"
                and request.capability != "session.read_sensitive"
            ):
                return PermissionDecision.DENY

        if request.resource_type == "execution_agent_collection":
            if request.capability != "execution_agent.list" or request.resource_scope != str(chat_session.user_id):
                return PermissionDecision.DENY

        if request.resource_type == "execution_agent" and request.capability == "execution_agent.select":
            try:
                agent_id = UUID(request.resource_scope)
            except ValueError:
                return PermissionDecision.DENY
            agent = await db.scalar(select(ExecutionAgent).where(
                ExecutionAgent.id == agent_id,
                ExecutionAgent.user_id == chat_session.user_id,
                ExecutionAgent.revoked_at.is_(None),
            ))
            if agent is None or agent.user_id != chat_session.user_id or agent.revoked_at is not None:
                return PermissionDecision.DENY

        if request.resource_type == "project":
            try:
                project_id = UUID(request.resource_scope)
            except ValueError:
                return PermissionDecision.DENY
            if (
                project_id != chat_session.project_id
                or not request.capability.startswith("coding.")
            ):
                return PermissionDecision.DENY

        if request.resource_type == "project_session_path":
            scope = _project_session_path_scope(request.resource_scope)
            if scope is None or request.capability not in {
                "file.provide", "filesystem.read", "session.files.read"
            }:
                return PermissionDecision.DENY
            project_id, target_id, _path = scope
            target = await db.get(ChatSession, target_id, populate_existing=True)
            if (
                chat_session.project_id is None
                or project_id != chat_session.project_id
                or target is None
                or target.user_id != chat_session.user_id
                or target.project_id != project_id
                or target.session_type != "chat"
                or (target.parent_session_id is not None and target.parent_session_id != chat_session.id)
            ):
                return PermissionDecision.DENY
            if policy == ToolExecutionPolicy.AUTO:
                return PermissionDecision.ALLOW

        if request.resource_type == "execution_administrator":
            try:
                scope = json.loads(request.resource_scope)
                agent_id = UUID(scope["agentId"])
                shell = "command" in scope
                if (
                    set(scope) - ({"requiredCategories"} if shell else set()) != ({"agentId", "currentDirectory", "command", "action"} if shell else {"agentId", "currentDirectory", "path"})
                    or scope["currentDirectory"] != chat_session.current_directory
                    or request.capability not in ({f"shell.{category}" for category in scope.get("requiredCategories", ["read", "write"])} if shell else {"filesystem.read", "filesystem.write"})
                    or not isinstance(scope["command" if shell else "path"], str)
                    or not scope["command" if shell else "path"]
                    or PurePosixPath(scope["currentDirectory"]).as_posix() != scope["currentDirectory"]
                    or not PurePosixPath(scope["currentDirectory"]).is_absolute()
                    or any(not PurePosixPath(path).is_absolute() or PurePosixPath(path).as_posix() != path for path in request.administrator_paths)
                ):
                    return PermissionDecision.DENY
                agent = await db.scalar(select(ExecutionAgent).where(
                    ExecutionAgent.id == agent_id, ExecutionAgent.user_id == chat_session.user_id,
                    ExecutionAgent.revoked_at.is_(None),
                ).execution_options(populate_existing=True))
                if agent is None or chat_session.preferred_execution_agent_id != agent_id or policy == ToolExecutionPolicy.DISABLED:
                    return PermissionDecision.DENY
                paths = (scope["currentDirectory"], *request.administrator_paths) if shell else request.administrator_paths
                if not shell and scope["path"] not in request.administrator_paths:
                    return PermissionDecision.DENY
                home = PurePosixPath(agent.home_directory) if agent.home_directory else None
                workspace = PurePosixPath(scope["currentDirectory"])
                zones = {"home" if home and _path_is_within(PurePosixPath(path), home) else "external" for path in paths}
                outside = any(not _path_is_within(PurePosixPath(path), workspace) for path in paths)
                if not request.administrator_analysis_complete:
                    zones.add("external")
                    outside = True
                if (
                    any(getattr(agent, f"{zone}_filesystem_policy") == "deny" for zone in zones)
                    or shell and any(not getattr(agent, f"{zone}_shell_enabled") for zone in zones)
                    or not shell and outside and external_working_directory_policy == ToolExecutionPolicy.DISABLED
                ):
                    return PermissionDecision.DENY
                if (decision := sensitive_decision(request.sensitive_access)) is not None:
                    return decision
                if policy == ToolExecutionPolicy.AUTO and (shell or not outside or external_working_directory_policy == ToolExecutionPolicy.AUTO):
                    return PermissionDecision.ALLOW
                grants = (await db.scalars(select(PermissionGrant).where(
                    PermissionGrant.session_id == grant_session_id,
                    PermissionGrant.capability.in_(request.grant_capabilities),
                    PermissionGrant.resource_type == request.effective_session_grants[0].resource_type,
                    PermissionGrant.resource_scope == request.effective_session_grants[0].resource_scope,
                    *grant_binding_conditions(),
                ))).all()
                return PermissionDecision.ALLOW if any(PermissionPolicy.grant_matches(grant, request, root_files) for grant in grants) else PermissionDecision.ASK
            except (KeyError, ValueError, TypeError):
                return PermissionDecision.DENY

        if request.resource_type == "execution_directory":
            scope = _execution_directory_scope(request.resource_scope)
            if scope is None:
                return PermissionDecision.DENY
            agent = await db.scalar(
                select(ExecutionAgent).where(
                    ExecutionAgent.id == scope[0],
                    ExecutionAgent.user_id == chat_session.user_id,
                    ExecutionAgent.revoked_at.is_(None),
                )
            )
            if agent is None:
                return PermissionDecision.DENY
            temporary_allowed = False
            if (
                agent.id == chat_session.preferred_execution_agent_id
                and request.capability in {"filesystem.read", "filesystem.write"}
            ):
                from mato_loop_backend.execution.manager import execution_agent_connections

                info = execution_agent_connections.system_info(agent.id)
                if info is not None and info.temporary_directory is not None:
                    temporary_allowed = any(PermissionPolicy.grant_matches(grant, request, root_files)
                                            for grant in temporary_file_grants(
                                                chat_session, agent.id, info.temporary_directory,
                                                writable=temporary_files_enabled,
                                            ))
            if policy == ToolExecutionPolicy.DISABLED and not temporary_allowed:
                return PermissionDecision.DENY
            if temporary_allowed:
                policy = ToolExecutionPolicy.ASK
            path = scope[1]
            home = agent.home_directory
            selected_policy = (
                agent.home_filesystem_policy
                if home is not None and _path_is_within(path, home)
                else agent.external_filesystem_policy
            )
            if selected_policy == "deny":
                return PermissionDecision.DENY
            workspace = (
                chat_session.current_directory
                if chat_session.current_directory is not None
                else None
            )
            inside_workspace = workspace is not None and _path_is_within(path, workspace)
            if not inside_workspace and external_working_directory_policy == ToolExecutionPolicy.DISABLED and not temporary_allowed:
                return PermissionDecision.DENY
            if (decision := sensitive_decision(
                request.sensitive_access or request.capability != "file.provide" and confidential_execution_path(str(path))
            )) is not None:
                return decision
            if temporary_allowed:
                return PermissionDecision.ALLOW
            if policy == ToolExecutionPolicy.AUTO and inside_workspace:
                return PermissionDecision.ALLOW
            if (
                policy == ToolExecutionPolicy.AUTO
                and external_working_directory_policy == ToolExecutionPolicy.AUTO
            ):
                return PermissionDecision.ALLOW
            grants = list(
                (
                    await db.scalars(
                        select(PermissionGrant).where(
                            PermissionGrant.session_id == grant_session_id,
                            PermissionGrant.capability.in_(request.grant_capabilities),
                            *grant_binding_conditions(),
                        )
                    )
                ).all()
            )
            if grants:
                request = await prepare_file_grants(request, root_files)
            return (
                PermissionDecision.ALLOW
                if any(PermissionPolicy.grant_matches(item, request, root_files) for item in grants)
                else PermissionDecision.ASK
            )

        if request.resource_type == "shell_command":
            scope = _shell_command_scope(request.resource_scope)
            if (
                scope is None
                or request.capability not in {f"shell.{category}" for category in scope.get("requiredCategories", [scope["classification"]])}
                or not request.shell_metadata_paths.issubset(scope["paths"])
            ):
                return PermissionDecision.DENY
            agent = await db.scalar(
                select(ExecutionAgent)
                .where(
                    ExecutionAgent.id == UUID(scope["agentId"]),
                    ExecutionAgent.user_id == chat_session.user_id,
                    ExecutionAgent.revoked_at.is_(None),
                )
                .execution_options(populate_existing=True)
            )
            if agent is None:
                return PermissionDecision.DENY
            home = agent.home_directory
            paths = [scope["currentDirectory"], *scope["paths"]]
            zones = [
                "home"
                if home is not None and _path_is_within(path, home)
                else "external"
                for path in paths
            ]
            if not scope["analysisComplete"]:
                zones.append("external")
            if any(not getattr(agent, f"{zone}_shell_enabled") for zone in zones):
                return PermissionDecision.DENY
            filesystem_policies = [
                getattr(agent, f"{zone}_filesystem_policy") for zone in zones
            ]
            if policy == ToolExecutionPolicy.DISABLED or "deny" in filesystem_policies:
                return PermissionDecision.DENY
            target_paths = paths
            confidential = any(
                confidential_execution_path(str(path)) for path in target_paths
            )
            if (decision := sensitive_decision(confidential or request.sensitive_access)) is not None:
                return decision
            if policy == ToolExecutionPolicy.AUTO:
                return PermissionDecision.ALLOW
            grants = list(
                (
                    await db.scalars(
                        select(PermissionGrant).where(
                            PermissionGrant.session_id == grant_session_id,
                            PermissionGrant.capability.in_(request.grant_capabilities),
                            *grant_binding_conditions(),
                        )
                    )
                ).all()
            )
            return PermissionDecision.ALLOW if any(
                PermissionPolicy.grant_matches(grant, request, root_files) for grant in grants
            ) else PermissionDecision.ASK

        if request.resource_type == "root_path":
            scope = _root_path_scope(request.resource_scope)
            if scope is None or root_files.session_id != chat_session.id:
                return PermissionDecision.DENY
            root, identity, path = scope
            try:
                current_identity = root_files.root_identity(root)
            except RootFileError:
                return PermissionDecision.DENY
            if identity != current_identity:
                return PermissionDecision.DENY
            if identity != str(chat_session.id):
                return PermissionDecision.DENY
            if request.capability not in {
                "filesystem.read",
                "filesystem.write",
                "filesystem.delete",
                "file.provide",
                "session.files.read",
                "session.files.write",
                "session.files.delete",
            }:
                return PermissionDecision.DENY
            if policy == ToolExecutionPolicy.DISABLED:
                return PermissionDecision.DENY
            if request.capability in {"filesystem.delete", "session.files.delete"} and policy is None:
                policy = ToolExecutionPolicy.ASK
            if session_write_paths is not None and root == FileRoot.SESSION and (
                request.capability in {"session.files.write", "session.files.delete", "filesystem.delete"}
                and (
                    str(path) not in session_write_paths
                    or request.action_kind in {"create_directory", "copy_path", "move_path"}
                )
            ):
                policy = ToolExecutionPolicy.DISABLED
            if (decision := sensitive_decision(request.sensitive_access)) is not None:
                return decision
            if policy in {None, ToolExecutionPolicy.AUTO}:
                return PermissionDecision.ALLOW

        elif request.resource_type != "project_session_path" and request.capability.startswith(
            ("filesystem.", "session.files.")
        ):
            return PermissionDecision.DENY

        if policy == ToolExecutionPolicy.DISABLED:
            return PermissionDecision.DENY
        if (decision := sensitive_decision(request.sensitive_access)) is not None:
            return decision
        if policy == ToolExecutionPolicy.AUTO:
            return PermissionDecision.ALLOW

        grants = list(
            (
                await db.scalars(
                        select(PermissionGrant).where(
                            PermissionGrant.session_id == grant_session_id,
                            PermissionGrant.capability.in_(request.grant_capabilities),
                            *grant_binding_conditions(),
                    )
                )
            ).all()
        )
        if request.resource_type == "root_path" and grants:
            request = await prepare_file_grants(request, root_files)
        for grant in grants:
            if PermissionPolicy.grant_matches(grant, request, root_files):
                return PermissionDecision.ALLOW
        return PermissionDecision.ASK

    @staticmethod
    def grant_matches(
        grant: PermissionGrant,
        request: PermissionRequest,
        root_files: RootFileService,
    ) -> bool:
        if grant.capability not in request.grant_capabilities:
            return False
        if grant.capability.startswith("category."):
            if request.resource_type == "shell_command" and grant.resource_type == "shell_computer":
                return session_grant_matches_request(
                    request.capability, request.resource_type, request.resource_scope,
                    SessionGrantScope(_access_for_capability(request.capability), "shell_computer", grant.resource_scope),
                )
            if request.resource_type == "shell_command" and grant.resource_type == "shell_action":
                scope = _shell_command_scope(request.resource_scope)
                return bool(scope and grant.resource_scope == scope.get("action"))
            return any(
                PermissionPolicy.scope_matches(grant, scope, root_files)
                and session_grant_matches_request(request.capability, request.resource_type, request.resource_scope, scope)
                for scope in request.effective_session_grants
            )
        if grant.resource_type != request.resource_type:
            return False
        if request.resource_type == "execution_directory":
            resource = _execution_directory_scope(request.resource_scope)
            granted = _execution_directory_scope(grant.resource_scope)
            return bool(
                resource
                and granted
                and request.capability == "execution.directory"
                and resource[0] == granted[0]
                and _path_is_within(resource[1], granted[1])
            )
        return False

    @staticmethod
    def scope_matches(
        grant: PermissionGrant,
        requested: SessionGrantScope,
        root_files: RootFileService,
    ) -> bool:
        if (
            not grant.capability.startswith("category.")
            or not category_covers(grant.capability.removeprefix("category."), requested.access)
            or grant.resource_type != requested.resource_type
        ):
            return False
        if requested.resource_type in {"session_path", "execution_path"}:
            try:
                if json.loads(requested.resource_scope).get("recursive") and not json.loads(grant.resource_scope).get("recursive"):
                    return False
            except (ValueError, TypeError, AttributeError):
                return False
        if requested.resource_type == "session_path":
            resource = _root_path_scope(requested.resource_scope)
            granted = _root_path_scope(grant.resource_scope)
            if resource is None or granted is None:
                return False
            resource_root, resource_identity, resource_path = resource
            grant_root, grant_identity, grant_path = granted
            try:
                current_identity = root_files.root_identity(resource_root)
            except RootFileError:
                return False
            return (
                resource_root is grant_root
                and resource_identity == grant_identity == current_identity
                and (resource_path.is_relative_to(grant_path) if json.loads(grant.resource_scope).get("recursive") else resource_path == grant_path)
            )
        if requested.resource_type in {"execution_path", "administrator_path"}:
            resource = _execution_directory_scope(requested.resource_scope)
            granted = _execution_directory_scope(grant.resource_scope)
            return bool(
                resource and granted and resource[0] == granted[0]
                and (_path_is_within(resource[1], granted[1])
                     if requested.resource_type == "execution_path" and json.loads(grant.resource_scope).get("recursive")
                     else _path_key(resource[1]) == _path_key(granted[1]))
            )
        if requested.resource_type == "integration_resource":
            return (
                _integration_resource_scope(requested.resource_scope) is not None
                and _integration_resource_scope(grant.resource_scope) is not None
                and requested.resource_scope == grant.resource_scope
            )
        return requested.resource_scope == grant.resource_scope
