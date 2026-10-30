from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mato_loop_backend.integrations.arcane.client import (
    ArcaneClient,
    ArcaneConfig,
    ArcaneError,
    environment_id,
    segment,
)
from mato_loop_backend.integrations.arcane.project_config import (
    MAX_CONFIG_CHARACTERS,
    MAX_CONFIG_RESPONSE_BYTES,
    configuration,
    fingerprint,
    project_snapshot,
    read_snapshot,
)
from mato_loop_backend.integrations.contracts import (
    IntegrationAdapterDefinition,
    IntegrationContributions,
    IntegrationResource,
    ToolContribution,
)
from mato_loop_backend.integrations.service import remember_resource_labels
from mato_loop_backend.models import Credential
from mato_loop_backend.permissions.policy import PermissionDecision
from mato_loop_backend.security import load_credential, read_credential
from mato_loop_backend.settings import IntegrationInstance
from mato_loop_backend.tools.actions import ToolAction
from mato_loop_backend.tools.arguments import ProtectedText
from mato_loop_backend.tools.contracts import ToolResult
from mato_loop_backend.tools.secrets import resolve_protected_text
from mato_loop_backend.tools.snapshots import ToolSnapshotPolicy

ARCANE_CREDENTIAL_KIND = "arcane.api_key"

_ACCOUNTS = ToolAction("arcane.accounts.read", "Arcane-Instanzen auflisten", category="read")
_READ = ToolAction("arcane.read", "Docker-Ressourcen lesen", category="read")
_MANAGE = ToolAction("arcane.manage", "Docker-Ressourcen verwalten", category="write", mutates=True)
_DELETE = ToolAction("arcane.delete", "Docker-Ressourcen löschen", category="delete", mutates=True)

Kind = Literal["containers", "projects", "images", "volumes", "networks"]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArcaneAccounts(_Args):
    pass


class ArcaneInstance(_Args):
    integration_id: UUID


class ArcaneEnvironment(ArcaneInstance):
    environment_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


class ArcaneList(ArcaneEnvironment):
    kind: Kind
    search: str | None = Field(default=None, max_length=160)
    start: int = Field(default=0, ge=0, le=100_000)
    limit: int = Field(default=20, ge=1, le=50)


class ArcaneGet(ArcaneEnvironment):
    kind: Kind
    resource_id: str = Field(min_length=1, max_length=256)
    view: Literal["summary", "runtime", "updates"] = "summary"

    @model_validator(mode="after")
    def validate_view(self) -> ArcaneGet:
        if self.kind != "projects" and self.view != "summary":
            raise ValueError("runtime and updates views require a project")
        return self


class ArcaneLogs(ArcaneEnvironment):
    container_id: str = Field(min_length=1, max_length=256)
    tail: int = Field(default=200, ge=1, le=500)


class ArcaneProjectConfig(ArcaneEnvironment):
    project_id: str = Field(min_length=1, max_length=256)


class ArcaneProjectSave(ArcaneEnvironment):
    project_id: str | None = Field(default=None, min_length=1, max_length=256)
    snapshot_token: str | None = Field(default=None, min_length=1, max_length=4_096)
    name: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    compose_content: Annotated[str, Field(min_length=1, max_length=MAX_CONFIG_CHARACTERS)] | ProtectedText | None = None
    env_content: Annotated[str, Field(max_length=MAX_CONFIG_CHARACTERS)] | ProtectedText | None = None
    override_content: Annotated[str, Field(max_length=MAX_CONFIG_CHARACTERS)] | ProtectedText | None = None

    @model_validator(mode="after")
    def validate_project(self) -> ArcaneProjectSave:
        if self.project_id is None:
            if not self.name or self.compose_content is None or self.snapshot_token is not None or self.override_content is not None:
                raise ValueError("a new Arcane project requires name and compose_content, without snapshot or override")
        elif self.snapshot_token is None or all(value is None for value in (self.name, self.compose_content, self.env_content, self.override_content)):
            raise ValueError("an Arcane project update requires snapshot_token and at least one changed field")
        return self


class ArcaneAction(ArcaneEnvironment):
    action: Literal[
        "start", "stop", "restart", "pause", "unpause", "redeploy",
        "up", "down", "archive", "unarchive", "pull", "build", "update_services",
        "create_container", "create_volume", "create_network", "pull_image", "tag_image",
        "rename_volume", "connect_network", "disconnect_network", "auto_update",
    ]
    resource_id: str | None = Field(default=None, min_length=1, max_length=256)
    name: str | None = Field(default=None, min_length=1, max_length=256)
    tag: str | None = Field(default=None, min_length=1, max_length=128)
    image: str | None = Field(default=None, min_length=1, max_length=256)
    enabled: bool | None = None

    @model_validator(mode="after")
    def validate_action(self) -> ArcaneAction:
        needs_id = self.action not in {"create_container", "create_volume", "create_network", "pull_image"}
        if needs_id != (self.resource_id is not None):
            raise ValueError("resource_id must match the selected Arcane action")
        if (self.action in {"create_container", "create_volume", "create_network", "pull_image", "tag_image", "rename_volume", "connect_network", "disconnect_network"}) != (self.name is not None):
            raise ValueError("name must match the selected Arcane action")
        if (self.action == "tag_image") != (self.tag is not None):
            raise ValueError("tag must match the selected Arcane action")
        if (self.action == "create_container") != (self.image is not None):
            raise ValueError("image must match the selected Arcane action")
        if (self.action == "auto_update") != (self.enabled is not None):
            raise ValueError("enabled must match the selected Arcane action")
        return self


class ArcaneDelete(ArcaneGet):
    kind: Literal["containers", "projects", "images", "volumes", "networks"]


def _credential(record: Credential | None) -> str:
    if record is None or record.kind != ARCANE_CREDENTIAL_KIND:
        raise ArcaneError("integration.authenticationFailed")
    try:
        payload = read_credential(record)
    except Exception as exc:
        raise ArcaneError("integration.authenticationFailed") from exc
    if not isinstance(payload, dict) or set(payload) != {"api_key"} or not isinstance(payload["api_key"], str):
        raise ArcaneError("integration.authenticationFailed")
    return payload["api_key"]


def _binding(context: Any, integration_id: UUID) -> tuple[IntegrationInstance, str]:
    revision = context.bound_options.get("binding_revision")
    if context.bound_options.get("integration_id") != str(integration_id) or not isinstance(revision, str):
        raise ValueError("Arcane integration is not bound")
    instance = next((item for item in context.integration_settings.instances if item.id == integration_id
                     and item.enabled and item.adapter_type == "arcane" and item.integration_type == "infrastructure"), None)
    if instance is None or json.dumps(instance.binding_data(), ensure_ascii=True, separators=(",", ":"), sort_keys=True) != revision:
        raise ValueError("Arcane integration binding is stale")
    return instance, revision


@asynccontextmanager
async def _client(arguments: ArcaneInstance, context: Any):
    instance, _revision = _binding(context, arguments.integration_id)
    record = await load_credential(context.db, instance.credential_id, owner_id=instance.owner_id) if instance.credential_id else None
    config = ArcaneConfig.model_validate(instance.config)
    async with ArcaneClient(str(config.base_url), _credential(record)) as client:
        yield client


async def _environments(client: ArcaneClient) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for start in range(0, 500, 50):
        page, _pagination = await client.request("GET", "/environments", params={"start": start, "limit": 50})
        if page is None:
            page = []
        if not isinstance(page, list) or len(page) > 50 or not all(isinstance(item, dict) for item in page):
            raise ArcaneError("integration.invalidResponse")
        result.extend(page)
        if len(page) < 50:
            return result
    raise ArcaneError("integration.responseTooLarge")


async def load_arcane_resources(instance: IntegrationInstance, credential: dict[str, Any] | None) -> tuple[IntegrationResource, ...]:
    if not isinstance(credential, dict) or set(credential) != {"api_key"}:
        raise ArcaneError("integration.authenticationFailed")
    config = ArcaneConfig.model_validate(instance.config)
    async with ArcaneClient(str(config.base_url), credential["api_key"]) as client:
        environments = await _environments(client)
    resources = []
    for item in environments:
        key = environment_id(item.get("id"))
        resources.append(IntegrationResource(key=key, label=str(item.get("name") or key)[:160]))
    return tuple(resources)


def _public(item: Any, kind: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ArcaneError("integration.invalidResponse")
    fields = {
        "environments": ("id", "name", "status", "enabled", "isEdge", "connected"),
        "containers": ("id", "name", "image", "state", "status", "created", "ports", "activityId"),
        "projects": ("id", "name", "status", "statusReason", "isArchived", "createdAt", "updatedAt", "runningCount", "serviceCount", "runtimeServices", "activityId"),
        "images": ("id", "repoTags", "size", "created", "inUse", "activityId"),
        "volumes": ("id", "name", "driver", "scope", "size", "inUse", "containers", "activityId"),
        "networks": ("id", "name", "driver", "scope", "inUse", "isDefault", "containersList", "activityId"),
    }[kind]
    result = {}
    for key in fields:
        if key not in item:
            continue
        value = item[key]
        if isinstance(value, str):
            result[key] = value[:500]
        elif isinstance(value, bool | int | float) or value is None:
            result[key] = value
        elif isinstance(value, list):
            result[key] = [entry[:500] if isinstance(entry, str) else
                           {field: data[:500] if isinstance(data, str) else data
                            for field, data in entry.items() if field in {"id", "name", "image", "status", "health", "containerId", "containerName", "ipv4Address", "ipv6Address", "privatePort", "publicPort", "type"}}
                           for entry in value[:40] if isinstance(entry, str | dict)]
    return result


def _label_key(args: ArcaneInstance, revision: str, kind: str, resource_id: str) -> str:
    binding = hashlib.sha256(revision.encode()).hexdigest()
    environment = getattr(args, "environment_id", "") if kind != "environments" else ""
    return f"{binding}:{environment}:{kind}:{resource_id}"


async def _remember_resources(args: ArcaneInstance, context: Any, kind: str, items: list[dict[str, Any]]) -> None:
    named = []
    for item in items:
        name = item.get("name")
        if kind == "images":
            tags = item.get("repoTags")
            name = ", ".join(tag for tag in tags[:3] if isinstance(tag, str)) if isinstance(tags, list) else None
        resource_id = item.get("id")
        if isinstance(resource_id, str | int) and not isinstance(resource_id, bool) and isinstance(name, str) and name.strip():
            named.append((str(resource_id), " ".join(name.split())[:80]))
    if named:
        instance, revision = _binding(context, args.integration_id)
        await remember_resource_labels(context, instance, {
            _label_key(args, revision, kind, resource_id): name for resource_id, name in named
        })


def _resource_label(args: ArcaneInstance, context: Any, kind: str, resource_id: str) -> str:
    _instance, revision = _binding(context, args.integration_id)
    labels = getattr(getattr(context, "tool_settings", None), "resource_labels", {})
    name = labels.get(str(args.integration_id), {}).get(_label_key(args, revision, kind, resource_id))
    return f"{name} ({resource_id})" if name and name != resource_id else resource_id


def _target_display(args: ArcaneInstance, context: Any, target: str) -> str:
    if isinstance(args, ArcaneAction):
        kind = _ACTION_ROUTES[args.action][0] if args.action in _ACTION_ROUTES else {
            "auto_update": "containers", "rename_volume": "volumes", "tag_image": "images",
            "connect_network": "networks", "disconnect_network": "networks",
        }.get(args.action)
        resource_id = args.resource_id
    elif isinstance(args, ArcaneLogs):
        kind, resource_id = "containers", args.container_id
    elif isinstance(args, ArcaneProjectConfig | ArcaneProjectSave):
        kind, resource_id = "projects", args.project_id
    else:
        kind, resource_id = getattr(args, "kind", None), getattr(args, "resource_id", None)
    if kind and resource_id:
        target = target.replace(resource_id, _resource_label(args, context, kind, resource_id), 1)
    if isinstance(args, ArcaneAction) and args.action in {"connect_network", "disconnect_network"}:
        target = target.rsplit(" → ", 1)[0] + " → " + _resource_label(args, context, "containers", args.name)
    return target


def _result(value: Any) -> Any:
    from mato_loop_backend.tools.contracts import ToolResult

    return ToolResult.json({"data": value, "contentIsUntrusted": True})


def _error(code: str) -> Any:
    from mato_loop_backend.tools.contracts import ToolResult

    return ToolResult.error(code)


async def arcane_accounts(_args: ArcaneAccounts, context: Any) -> Any:
    ids = set(context.bound_options.get("integration_ids", ()))
    return _result([{"integrationId": str(item.id), "name": item.name[:160]} for item in context.integration_settings.instances
                    if str(item.id) in ids and item.enabled and item.adapter_type == "arcane"])


async def arcane_environments(args: ArcaneInstance, context: Any) -> Any:
    try:
        async with _client(args, context) as client:
            items = await _environments(client)
        safe = [_public(item, "environments") for item in items]
        await _remember_resources(args, context, "environments", safe)
        return _result(safe)
    except ValueError:
        return _error("integration.staleReference")
    except ArcaneError as exc:
        return _error(exc.code)


async def arcane_list(args: ArcaneList, context: Any) -> Any:
    try:
        async with _client(args, context) as client:
            data, pagination = await client.request("GET", f"/environments/{args.environment_id}/{args.kind}",
                                                    params={"start": args.start, "limit": args.limit, **({"search": args.search} if args.search else {})})
        if data is None:
            data = []
        if not isinstance(data, list) or len(data) > args.limit:
            raise ArcaneError("integration.invalidResponse")
        safe = [_public(item, args.kind) for item in data]
        await _remember_resources(args, context, args.kind, safe)
        return _result({"items": safe, "pagination": pagination})
    except ValueError:
        return _error("integration.staleReference")
    except ArcaneError as exc:
        return _error(exc.code)


async def arcane_get(args: ArcaneGet, context: Any) -> Any:
    try:
        async with _client(args, context) as client:
            path = f"/environments/{args.environment_id}/{args.kind}/{segment(args.resource_id)}"
            data, _ = await client.request("GET", path + (f"/{args.view}" if args.view != "summary" else ""))
        safe = _public(data, args.kind)
        await _remember_resources(args, context, args.kind, [{**safe, "id": args.resource_id}])
        if args.view == "updates" and isinstance(data.get("updateInfo"), dict):
            info = data["updateInfo"]
            safe["updateInfo"] = {key: info[key] for key in ("status", "hasUpdate", "imageCount", "imagesWithUpdates", "imagesNotPulled", "errorCount")
                                  if key in info and isinstance(info[key], str | bool | int)}
        return _result(safe)
    except ValueError:
        return _error("integration.staleReference")
    except ArcaneError as exc:
        return _error(exc.code)


async def arcane_logs(args: ArcaneLogs, context: Any) -> Any:
    try:
        async with _client(args, context) as client:
            logs = await client.logs(args.environment_id, args.container_id, args.tail)
        return _result({"containerId": args.container_id, **logs})
    except ValueError:
        return _error("integration.staleReference")
    except ArcaneError as exc:
        return _error(exc.code)


def _project_snapshot_context(args: ArcaneEnvironment, context: Any, project_id: str) -> dict[str, str]:
    _instance, revision = _binding(context, args.integration_id)
    return {"integration_id": str(args.integration_id), "environment_id": args.environment_id,
            "project_id": project_id, "session_id": str(context.session.id), "binding_revision": revision}


async def arcane_project_config(args: ArcaneProjectConfig, context: Any) -> Any:
    try:
        binding = _project_snapshot_context(args, context, args.project_id)
        async with _client(args, context) as client:
            data, _ = await client.request("GET", f"/environments/{args.environment_id}/projects/{segment(args.project_id)}/compose", max_bytes=MAX_CONFIG_RESPONSE_BYTES)
        snapshot = configuration(data)
        await _remember_resources(args, context, "projects", [{"id": args.project_id, "name": snapshot.get("name")}])
        return _result(project_snapshot(snapshot, **binding))
    except ValueError:
        return _error("integration.staleReference")
    except ArcaneError as exc:
        return _error(exc.code)


async def arcane_project_save(args: ArcaneProjectSave, context: Any) -> Any:
    try:
        snapshot = read_snapshot(args.snapshot_token, **_project_snapshot_context(args, context, args.project_id)) if args.project_id else None
        body = {"name": args.name} if args.name is not None else {}
        for field, remote in (("compose_content", "composeContent"), ("env_content", "envContent"), ("override_content", "overrideContent")):
            value = getattr(args, field)
            if value is None:
                continue
            resolved = await resolve_protected_text(context, value)
            if isinstance(resolved, ToolResult):
                return resolved
            text, _protected = resolved
            if len(text) > MAX_CONFIG_CHARACTERS or remote == "composeContent" and not text.strip():
                return _error("tool.invalidArguments")
            if args.project_id and not text.strip() and isinstance(value, ProtectedText):
                return _error("tool.invalidArguments")
            body[remote] = text
        context.integration_settings = await context.current_integrations()
        for request in _project_save_permission(args, context):
            if await context.ask_handler(request, check_only=True) != PermissionDecision.ALLOW:
                return _error("permission.denied")
        path = f"/environments/{args.environment_id}/projects"
        async with _client(args, context) as client:
            current_config = {}
            if args.project_id:
                path += f"/{segment(args.project_id)}"
                current, _ = await client.request("GET", path + "/compose", max_bytes=MAX_CONFIG_RESPONSE_BYTES)
                current_config = configuration(current)
                # ponytail: Arcane has no conditional config write; a remote edit between GET and PUT can still race.
                if fingerprint(current_config) != snapshot["configurationDigest"]:
                    return _error("integration.changed")
            try:
                configuration({**current_config, **body})
            except ArcaneError:
                return _error("tool.invalidArguments")
            if args.project_id:
                data, _ = await client.request("PUT", path, body=body, max_bytes=MAX_CONFIG_RESPONSE_BYTES)
            else:
                data, _ = await client.request("POST", path, multipart={"project": body, "manifest": {"fileChanges": []}})
        project = _public(data, "projects")
        await _remember_resources(args, context, "projects", [project])
        return _result({"saved": True, "project": project, "deploymentRequired": True})
    except ValueError:
        return _error("integration.staleReference")
    except ArcaneError as exc:
        return _error(exc.code)


_ACTION_ROUTES = {
    "start": ("containers", "start", False), "stop": ("containers", "stop", False),
    "restart": ("containers", "restart", False), "pause": ("containers", "pause", False),
    "unpause": ("containers", "unpause", False), "redeploy": ("containers", "redeploy", False),
    "up": ("projects", "up", True), "down": ("projects", "down", False),
    "archive": ("projects", "archive", False), "unarchive": ("projects", "unarchive", False),
    "pull": ("projects", "pull", True), "build": ("projects", "build", True),
    "update_services": ("projects", "update-services", False),
}

_VERIFY_PROJECT_ACTIONS = frozenset({"up", "update_services"})


async def arcane_action(args: ArcaneAction, context: Any) -> Any:
    try:
        env = f"/environments/{args.environment_id}"
        body = None
        streamed = False
        if args.action in _ACTION_ROUTES:
            kind, action, streamed = _ACTION_ROUTES[args.action]
            path = f"{env}/{kind}/{segment(args.resource_id or '')}/{action}"
        elif args.action == "create_container":
            # ponytail: basic containers only; add typed mounts and ports when a real workflow needs them.
            path, body = f"{env}/containers", {"name": args.name, "image": args.image}
        elif args.action == "create_volume":
            path, body = f"{env}/volumes", {"name": args.name}
        elif args.action == "create_network":
            path, body = f"{env}/networks", {"name": args.name, "options": {"driver": "bridge", "checkDuplicate": True}}
        elif args.action == "pull_image":
            path, body, streamed = f"{env}/images/pull", {"imageName": args.name}, True
        elif args.action == "rename_volume":
            path, body = f"{env}/volumes/{segment(args.resource_id or '')}/rename", {"name": args.name}
        elif args.action in {"connect_network", "disconnect_network"}:
            suffix = "connect" if args.action == "connect_network" else "disconnect"
            path, body = f"{env}/networks/{segment(args.resource_id or '')}/{suffix}", {"containerId": args.name}
        elif args.action == "auto_update":
            path, body = f"{env}/containers/{segment(args.resource_id or '')}/auto-update", {"enabled": args.enabled}
        else:
            path, body = f"{env}/images/{segment(args.resource_id or '')}/tag", {"repository": args.name, "tag": args.tag}
        async with _client(args, context) as client:
            response = await client.request("PUT" if args.action == "auto_update" else "POST", path, body=body, streamed=streamed)
            result = response if streamed else {"completed": True, "activityId": (response[0] or {}).get("activityId") if isinstance(response[0], dict) else None}
            if args.action in _VERIFY_PROJECT_ACTIONS:
                try:
                    runtime, _ = await client.request("GET", f"{env}/projects/{segment(args.resource_id or '')}/runtime")
                    result["runtime"] = _public(runtime, "projects")
                except ArcaneError as exc:
                    # A failed read must not turn a confirmed deployment into a retryable mutation failure.
                    result["runtimeError"] = exc.code
        return _result(result)
    except ValueError:
        return _error("integration.staleReference")
    except ArcaneError as exc:
        return _error(exc.code)


async def arcane_delete(args: ArcaneDelete, context: Any) -> Any:
    try:
        path = f"/environments/{args.environment_id}/{args.kind}/{segment(args.resource_id)}"
        if args.kind == "projects":
            path += "/destroy"
        async with _client(args, context) as client:
            await client.request("DELETE", path, body={"removeFiles": True, "removeVolumes": False} if args.kind == "projects" else None)
        return _result({"deleted": True})
    except ValueError:
        return _error("integration.staleReference")
    except ArcaneError as exc:
        return _error(exc.code)


def _accounts_permission(_args: ArcaneAccounts, _context: Any) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import PermissionRequest

    return (PermissionRequest(capability=_ACCOUNTS.capability, resource_type="network_service", resource_scope="arcane_accounts",
                              action_kind="arcane_accounts", risk_level="medium", title="Arcane-Instanzen auflisten?",
                              summary="Der Agent möchte die Arcane-Instanzen lesen.", resource_display="Arcane-Instanzen"),)


def _permission(args: ArcaneInstance, context: Any, capability: str, operation: str, target: str, *, sensitive_access: bool = False) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import (
        PermissionDisplayItem,
        integration_operation_permission,
    )

    instance, revision = _binding(context, args.integration_id)
    material = args.model_dump_json(exclude_unset=True)
    target_key = hashlib.sha256(target.encode()).hexdigest()[:24]
    target_display = _target_display(args, context, target)
    environment = _resource_label(args, context, "environments", args.environment_id) if isinstance(args, ArcaneEnvironment) else "Instanz"
    label = {
        "start": "Container starten", "stop": "Container stoppen", "restart": "Container neu starten",
        "pause": "Container pausieren", "unpause": "Container fortsetzen", "redeploy": "Container erneut bereitstellen",
        "up": "Projekt starten", "down": "Projekt stoppen", "archive": "Projekt archivieren",
        "unarchive": "Projekt wiederherstellen", "pull": "Projektabbilder laden", "build": "Projektabbilder bauen",
        "update_services": "Projektdienste aktualisieren", "create_container": "Container erstellen",
        "create_volume": "Volume erstellen", "create_network": "Netzwerk erstellen",
        "pull_image": "Abbild laden", "tag_image": "Abbild markieren", "rename_volume": "Volume umbenennen",
        "connect_network": "Container mit Netzwerk verbinden", "disconnect_network": "Container vom Netzwerk trennen",
        "auto_update": "Automatische Containerupdates ändern",
    }.get(operation, operation)
    return (replace(integration_operation_permission(
        capability=capability, integration_id=args.integration_id, binding_revision=revision,
        operation_key=f"arcane:{hashlib.sha256(material.encode()).hexdigest()}",
        resource_key=getattr(args, "environment_id", None),
        grant_resource_key=f"{getattr(args, 'environment_id', 'account')}:{target_key}",
        resource_display=f'Arcane "{instance.name[:100]}" · {environment}',
        title=f"Arcane: {label}?", summary=f"Der Agent möchte {label.lower()}: {target_display}.",
        display_items=(PermissionDisplayItem("Ziel", target_display),),
    ), sensitive_access=sensitive_access),)


def _environments_permission(args: ArcaneInstance, context: Any) -> tuple[Any, ...]:
    return _permission(args, context, _READ.capability, "Umgebungen lesen", "Umgebungen")


def _read_permission(args: ArcaneList | ArcaneGet, context: Any) -> tuple[Any, ...]:
    return _permission(args, context, _READ.capability, "Ressourcen lesen", f"{args.kind}: {getattr(args, 'resource_id', 'Liste')}")


def _logs_permission(args: ArcaneLogs, context: Any) -> tuple[Any, ...]:
    return _permission(args, context, _READ.capability, "Protokoll lesen", f"Container: {args.container_id}")


def _manage_permission(args: ArcaneAction, context: Any) -> tuple[Any, ...]:
    target = f"{args.resource_id or args.name}"
    if args.resource_id and args.name:
        target += f" → {args.name}"
    if args.image:
        target += f" · {args.image}"
    if args.tag:
        target += f":{args.tag}"
    if args.enabled is not None:
        target += f" · {'einschalten' if args.enabled else 'ausschalten'}"
    requests = _permission(args, context, _MANAGE.capability, args.action, target)
    if args.action in _VERIFY_PROJECT_ACTIONS:
        requests += _permission(args, context, _READ.capability, "Projektzustand prüfen", target)
    return requests


def _project_config_permission(args: ArcaneProjectConfig, context: Any) -> tuple[Any, ...]:
    return _permission(args, context, _READ.capability, "Projektkonfiguration lesen", args.project_id, sensitive_access=True)


def _project_save_permission(args: ArcaneProjectSave, context: Any) -> tuple[Any, ...]:
    from mato_loop_backend.permissions.policy import PermissionDisplayItem

    if args.project_id:
        read_snapshot(args.snapshot_token, **_project_snapshot_context(args, context, args.project_id))
    request = _permission(args, context, _MANAGE.capability, "Projektkonfiguration speichern", args.project_id or args.name)[0]
    previews = []
    for field, label in (("compose_content", "Compose"), ("env_content", "Umgebung"), ("override_content", "Compose-Override")):
        value = getattr(args, field)
        if value is not None:
            text = value if isinstance(value, str) else json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
            previews.append(PermissionDisplayItem(label, text, "code"))
    if args.name is not None:
        previews.append(PermissionDisplayItem("Projektname", args.name))
    requests = (replace(request, display_items=(*request.display_items, *previews)),)
    if args.project_id:
        requests += _permission(args, context, _READ.capability, "Projektkonfiguration prüfen", args.project_id, sensitive_access=True)
        if any(isinstance(value, str) and not value.strip() for value in (args.env_content, args.override_content)):
            requests += _permission(args, context, _DELETE.capability, "Projektkonfiguration leeren", args.project_id)
    return requests


def _delete_permission(args: ArcaneDelete, context: Any) -> tuple[Any, ...]:
    return _permission(args, context, _DELETE.capability, "Löschen", f"{args.kind}: {args.resource_id}")


ARCANE_ADAPTER = IntegrationAdapterDefinition(
    integration_type="infrastructure", adapter_type="arcane", config_model=ArcaneConfig,
    contribution_factory=lambda _instance: IntegrationContributions(),
    resource_loader=load_arcane_resources,
    resource_actions=(_READ, _MANAGE, _DELETE),
    shared_tools=(
        ToolContribution(name="arcane_accounts", description="List configured Arcane instances.", arguments_model=ArcaneAccounts,
                         snapshot_policy=ToolSnapshotPolicy("data", ("integrationId", "name"), max_age_seconds=None),
                         handler=arcane_accounts, label="Arcane-Instanzen auflisten", icon="dns", actions=(_ACCOUNTS,),
                         authorization_factory=_accounts_permission, releases_private_data=True),
        ToolContribution(name="arcane_environments", description="List accessible Docker environments in one Arcane instance.",
                         snapshot_policy=ToolSnapshotPolicy("data", ("id", "name")),
                         arguments_model=ArcaneInstance, handler=arcane_environments, label="Arcane-Umgebungen auflisten", icon="dns",
                         actions=(_READ,), instance_argument="integration_id", authorization_factory=_environments_permission, releases_private_data=True),
        ToolContribution(name="arcane_list", description="List containers, projects, images, volumes, or networks in one environment.",
                         arguments_model=ArcaneList, handler=arcane_list, label="Docker-Ressourcen auflisten", icon="list",
                         actions=(_READ,), instance_argument="integration_id", resource_argument="environment_id",
                         authorization_factory=_read_permission, releases_private_data=True),
        ToolContribution(name="arcane_get", description="Read one Docker resource. Projects also support runtime and image-update views.",
                         arguments_model=ArcaneGet, handler=arcane_get, label="Docker-Ressource lesen", icon="info",
                         actions=(_READ,), instance_argument="integration_id", resource_argument="environment_id",
                          authorization_factory=_read_permission, releases_private_data=True),
        ToolContribution(name="arcane_project_config", description="Read Compose, environment and override content plus included Compose files. Requires sensitive read access. Pass snapshotToken to arcane_project_save for updates.",
                         arguments_model=ArcaneProjectConfig, handler=arcane_project_config, label="Compose-Projektkonfiguration lesen", icon="description",
                         actions=(_READ,), instance_argument="integration_id", resource_argument="environment_id",
                         authorization_factory=_project_config_permission, releases_private_data=True),
        ToolContribution(name="arcane_project_save", description="Create or patch a Compose project; deploy separately with arcane_action up. Create: name and compose_content. Update: project_id and snapshot_token from arcane_project_config; omitted fields stay unchanged. Use protected text value_ref from secret_input or variable_read for new secrets. Explicit empty env_content/override_content clears/removes the file and also requires delete permission for updates. Protected text cannot clear an existing file. Included files are read-only.",
                         arguments_model=ArcaneProjectSave, handler=arcane_project_save, label="Compose-Projektkonfiguration speichern", icon="save",
                         actions=(_MANAGE, _READ, _DELETE), alternative_action_groups=(frozenset({_MANAGE.capability, _READ.capability, _DELETE.capability}),),
                         instance_argument="integration_id", resource_argument="environment_id",
                         authorization_factory=_project_save_permission, releases_private_data=True),
        ToolContribution(name="arcane_logs", description="Read a snapshot of the last tail container log lines, bounded to 32 KiB. A finished Arcane stream has no end marker; captureComplete=false means capture stopped after an idle or time bound, not proof of complete logs.",
                         arguments_model=ArcaneLogs, handler=arcane_logs, label="Containerprotokoll lesen", icon="article",
                         actions=(_READ,), instance_argument="integration_id", resource_argument="environment_id",
                         authorization_factory=_logs_permission, releases_private_data=True),
        ToolContribution(name="arcane_action", description="Run one named Docker lifecycle action or create a basic container, volume, or network. Project up and update_services include an authorized runtime status/health snapshot. Completed deployment does not imply healthy services; reread arcane_get runtime when health is still starting or runtimeError is present. Never repeat a confirmed mutation just because its status check failed.",
                         arguments_model=ArcaneAction, handler=arcane_action, label="Docker-Ressource verwalten", icon="settings",
                         actions=(_MANAGE, _READ), alternative_action_groups=(frozenset({_MANAGE.capability, _READ.capability}),), instance_argument="integration_id", resource_argument="environment_id",
                         authorization_factory=_manage_permission, releases_private_data=True),
        ToolContribution(name="arcane_delete", description="Delete a container, project, image, volume, or network.",
                         arguments_model=ArcaneDelete, handler=arcane_delete, label="Docker-Ressource löschen", icon="delete",
                         actions=(_DELETE,), instance_argument="integration_id", resource_argument="environment_id",
                         authorization_factory=_delete_permission, releases_private_data=True),
    ),
)
