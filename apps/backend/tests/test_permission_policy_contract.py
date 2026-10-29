import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

import mato_loop_backend.agent.loop as mock_agent_loop
import mato_loop_backend.agent.run_support as mock_agent_run_support
import mato_loop_backend.agent.runtime as mock_agent_runtime
import mato_loop_backend.integrations.arcane.adapter as arcane_module
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.agent.subagents import apply_delegation_ceiling, permission_snapshot
from mato_loop_backend.execution import (
    ExecutionClient,
    ExecutionClientError,
    ShellInspectResult,
    WorkspaceStatResult,
)
from mato_loop_backend.execution.manager import execution_agent_connections
from mato_loop_backend.execution.protocol import SystemInfo
from mato_loop_backend.files.service import RootFileService
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.models import (
    AgentRun,
    Automation,
    ChatSession,
    ExecutionAgent,
    Message,
    PermissionGrant,
)
from mato_loop_backend.permissions.policy import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    SessionGrantScope,
    administrator_permission,
    agent_identity_permission,
    automation_collection_scope,
    automation_scope,
    execution_agent_select_permission,
    execution_agents_list_permission,
    execution_directory_permission,
    execution_directory_scope,
    integration_operation_permission,
    media_permission,
    memory_write_permission,
    path_permission,
    project_session_path_permission,
    session_browse_permission,
    session_grant_options,
    session_grant_options_payload,
    session_grants_display,
    session_permission,
    session_search_permission,
    shell_action_scope,
    shell_command_permissions,
    shell_command_scope,
    skill_edit_permission,
    skill_load_permission,
    skill_write_permission,
    transcript_permission,
    user_profile_permission,
    webfetch_permission,
)
from mato_loop_backend.permissions.requests import prepare_file_grants
from mato_loop_backend.permissions.resolution import resolve_tool_execution_policy
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings import (
    GENERAL_PROFILE_ID,
    AgentProfilesSettings,
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
    ToolExecutionPolicy,
    ToolSettings,
    TrustLevel,
)
from mato_loop_backend.settings.models import (
    AgentProfilePermissions,
    IntegrationPermissions,
    sysadmin_profile,
)
from mato_loop_backend.tools.actions import ToolAction
from mato_loop_backend.tools.catalog import tool_registry


def _session(**values) -> ChatSession:
    defaults = {
        "id": uuid4(),
        "title": "Test",
        "memory_mode": "disabled",
        "web_search_policy": "available",
    }
    return ChatSession(**(defaults | values))


def _db_with_grants(*grants: PermissionGrant) -> AsyncMock:
    result = Mock()
    result.all.return_value = list(grants)
    db = AsyncMock(spec=AsyncSession)
    db.scalars.return_value = result
    db.get.return_value = _session()
    return db


def _grant(request: PermissionRequest, session_id=None, **values) -> PermissionGrant:
    scope = request.effective_session_grants[0]
    return PermissionGrant(
        session_id=session_id or uuid4(),
        capability=scope.capability,
        resource_type=scope.resource_type,
        resource_scope=scope.resource_scope,
        **values,
    )


def _tool_settings(data: dict) -> ToolSettings:
    values = dict(data)
    permissions = {
        key: values.pop(key)
        for key in ("policies", "integrationPolicies", "resourcePolicies")
        if key in values
    }
    values["publicPermissions"] = permissions
    return ToolSettings.model_validate(values).select_trust(TrustLevel.PUBLIC)


def _profile_settings(data: dict) -> AgentProfilesSettings:
    permissions = {
        key: data[key]
        for key in ("policies", "integrationPolicies", "resourcePolicies")
        if key in data
    }
    return AgentProfilesSettings.model_validate(
        {
            "builtin_access_overrides": {
                str(GENERAL_PROFILE_ID): {
                    "privatePermissions": permissions,
                    "publicPermissions": permissions,
                }
            }
        }
    )


def _root_files(tmp_path, session: ChatSession) -> RootFileService:
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    return RootFileService(
        session.id,
        session_storage=storage,
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_integration_category_grants_reuse_the_same_resource(tmp_path, legacy) -> None:
    integration_id = uuid4()
    regular = integration_operation_permission(
        capability="browser.use",
        integration_id=integration_id,
        binding_revision="{}",
        operation_key="click",
        grant_resource_key="browser:one:attachment:1",
        title="Browser verwenden?",
        summary="Browseraktion",
    )
    code = integration_operation_permission(
        capability="browser.code",
        integration_id=integration_id,
        binding_revision="{}",
        operation_key="run-code",
        grant_resource_key="browser:one:attachment:1",
        title="Browser-Code ausführen?",
        summary="Codeausführung",
    )
    session = _session()
    grant = _grant(regular, session.id)
    if legacy:
        grant.capability = "resource.write"
        grant.resource_scope = json.dumps({**json.loads(grant.resource_scope), "capability": regular.capability}, separators=(",", ":"), sort_keys=True)
    root_files = _root_files(tmp_path, session)

    assert PermissionPolicy.grant_matches(grant, regular, root_files) is not legacy
    assert PermissionPolicy.grant_matches(grant, code, root_files) is not legacy


@pytest.mark.parametrize(
    ("capability", "access"),
    [("mcp.call", "write"), ("sonobin.call", "write"), ("support.read", "read")],
)
def test_integration_session_grants_match_only_the_bound_resource(
    tmp_path, capability, access
) -> None:
    session = _session()
    root_files = _root_files(tmp_path, session)
    integration_id = uuid4()

    def request(*, resource_key="tool:one", selected_id=integration_id, revision="{}"):
        return integration_operation_permission(
            capability=capability,
            integration_id=selected_id,
            binding_revision=revision,
            operation_key="call",
            resource_key=resource_key,
            title="Tool aufrufen?",
            summary="Tool aufrufen.",
        )

    granted = request()
    assert granted.effective_session_grants[0].access == access
    grant = _grant(granted, session.id)
    assert PermissionPolicy.grant_matches(grant, granted, root_files)
    assert not PermissionPolicy.grant_matches(grant, request(resource_key="tool:two"), root_files)
    assert not PermissionPolicy.grant_matches(grant, request(selected_id=uuid4()), root_files)
    assert not PermissionPolicy.grant_matches(grant, request(revision='{"changed":true}'), root_files)


def _path_request(
    root_files: RootFileService,
    path: str,
    *,
    root: str = "session",
    capability: str = "filesystem.read",
):
    return path_permission(
        root_files=root_files,
        root=root,
        capability=capability,
        relative_path=path,
        action_kind="read_file",
        action_label="Datei lesen",
    )


@pytest.mark.parametrize("legacy", [False, True])
async def test_coding_permission_is_scoped_to_current_project(tmp_path, legacy) -> None:
    project_id = uuid4()
    session = _session(user_id=uuid4(), project_id=project_id)
    root_files = _root_files(tmp_path, session)
    db = _db_with_grants()
    request = PermissionRequest(
        capability="coding.analyze",
        resource_type="project",
        resource_scope=str(project_id),
        action_kind="coding.analyze",
        risk_level="medium",
        title="Analyze?",
        summary="Analyze project.",
    )

    assert await PermissionPolicy.evaluate(
        db, session, request, root_files, ToolExecutionPolicy.AUTO
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        db,
        session,
        replace(request, resource_scope=str(uuid4())),
        root_files,
        ToolExecutionPolicy.AUTO,
    ) == PermissionDecision.DENY

    grant = _grant(request)
    if legacy:
        grant.capability = "resource.read"
        grant.resource_scope = json.dumps(
            {"capability": request.capability, "projectId": str(project_id)}, separators=(",", ":"), sort_keys=True,
        )
    assert [PermissionPolicy.grant_matches(grant, target, root_files) for target in (
        request, replace(request, capability="coding.plan.read"),
    )] == [not legacy, not legacy]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("path, outside", [
    ("files/docs", "files/docs-other/readme.md"), ("C:/Public", "C:/public/private.txt"),
])
def test_root_path_grant_matches_only_same_root_identity_and_path(tmp_path, legacy, path, outside) -> None:
    session = _session()
    root_files = _root_files(tmp_path, session)
    granted = _path_request(root_files, path)
    grant = _grant(granted, session.id)
    if legacy:
        grant.capability = "resource.read"
        grant.resource_scope = granted.resource_scope

    assert PermissionPolicy.grant_matches(
        grant, _path_request(root_files, path), root_files
    ) is not legacy
    assert PermissionPolicy.grant_matches(
        grant, _path_request(root_files, f"{path}/readme.md"), root_files
    ) is not legacy
    assert not PermissionPolicy.grant_matches(
        grant, _path_request(root_files, outside), root_files
    )


async def test_managed_read_reuses_saved_filesystem_grant(tmp_path) -> None:
    session = _session()
    root_files = _root_files(tmp_path, session)
    request = _path_request(root_files, "notes.txt")
    assert request.capability == "session.files.read"
    grant = _grant(request, session.id)
    db = _db_with_grants(grant)
    assert await PermissionPolicy.evaluate(
        db, session, request, root_files, ToolExecutionPolicy.ASK
    ) == PermissionDecision.ALLOW
    assert ["category.read", "category.write"] in db.scalars.await_args.args[0].compile().params.values()
    assert await PermissionPolicy.evaluate(
        db, session, request, root_files, ToolExecutionPolicy.DISABLED
    ) == PermissionDecision.DENY


async def test_profile_session_write_paths_restrict_actual_permission(tmp_path) -> None:
    session = _session()
    root_files = _root_files(tmp_path, session)
    db = _db_with_grants()
    expected = _path_request(
        root_files,
        "plan.md",
        capability="session.files.write",
    )
    other = _path_request(
        root_files,
        "notes.md",
        capability="session.files.write",
    )

    assert await PermissionPolicy.evaluate(
        db,
        session,
        expected,
        root_files,
        ToolExecutionPolicy.AUTO,
        session_write_paths=["plan.md"],
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        db,
        session,
        other,
        root_files,
        ToolExecutionPolicy.AUTO,
        session_write_paths=["plan.md"],
    ) == PermissionDecision.DENY
    assert await PermissionPolicy.evaluate(
        db, session, expected, root_files, ToolExecutionPolicy.AUTO,
        session_write_paths=[],
    ) == PermissionDecision.DENY
    assert await PermissionPolicy.evaluate(
        db, session, other, root_files, ToolExecutionPolicy.AUTO,
        session_write_paths=None,
    ) == PermissionDecision.ALLOW
    delete_other = _path_request(
        root_files, "notes.md", capability="filesystem.delete"
    )
    create_plan_directory = replace(expected, action_kind="create_directory")
    copy_to_plan = replace(expected, action_kind="copy_path")
    assert await PermissionPolicy.evaluate(
        db, session, delete_other, root_files, ToolExecutionPolicy.AUTO,
        session_write_paths=["plan.md"],
    ) == PermissionDecision.DENY
    for request in (create_plan_directory, copy_to_plan):
        assert await PermissionPolicy.evaluate(
            db, session, request, root_files, ToolExecutionPolicy.AUTO,
            session_write_paths=["plan.md"],
        ) == PermissionDecision.DENY


@pytest.mark.parametrize("trust_level", list(TrustLevel))
@pytest.mark.parametrize("policy", [None, *ToolExecutionPolicy])
async def test_managed_deletion_honors_policy_grants_and_disabled(
    tmp_path, trust_level, policy
) -> None:
    session = _session()
    root_files = _root_files(tmp_path, session)
    request = _path_request(root_files, "output.txt", capability="filesystem.delete")
    assert not request.sensitive_access
    assert "allow_session" in request.available_decisions
    disabled = policy == ToolExecutionPolicy.DISABLED
    assert await PermissionPolicy.evaluate(
        _db_with_grants(), session, request, root_files, policy, trust_level=trust_level
    ) == (PermissionDecision.DENY if disabled else PermissionDecision.ALLOW if policy == ToolExecutionPolicy.AUTO else PermissionDecision.ASK)
    grant = _grant(request, session.id, trust_level=trust_level.value)
    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant), session, request, root_files, policy, trust_level=trust_level
    ) == (PermissionDecision.DENY if disabled else PermissionDecision.ALLOW)


def test_execution_directory_grant_matches_only_same_agent_subtree(tmp_path) -> None:
    session = _session()
    root_files = _root_files(tmp_path, session)
    agent_id = uuid4()
    granted = execution_directory_permission(
        agent_id, "/home/user/project"
    )
    assert granted.available_decisions == ("allow_once", "allow_session", "deny")
    grant = PermissionGrant(
        session_id=session.id,
        capability=granted.capability,
        resource_type=granted.resource_type,
        resource_scope=granted.resource_scope,
    )

    assert PermissionPolicy.grant_matches(
        grant, execution_directory_permission(agent_id, "/home/user/project"), root_files
    )
    assert PermissionPolicy.grant_matches(
        grant,
        execution_directory_permission(
            agent_id, "/home/user/project/src"
        ),
        root_files,
    )
    assert not PermissionPolicy.grant_matches(
        grant,
        execution_directory_permission(
            agent_id, "/home/user/project-other"
        ),
        root_files,
    )
    assert not PermissionPolicy.grant_matches(
        grant, _path_request(root_files, "docs/readme.md", root="session"), root_files
    )
    assert not PermissionPolicy.grant_matches(grant, execution_directory_permission(uuid4(), "/home/user/project/src"), root_files)
    assert not PermissionPolicy.grant_matches(grant, execution_directory_permission(agent_id, "/home/user"), root_files)
    assert not PermissionPolicy.grant_matches(grant, execution_directory_permission(agent_id, "/home/user/project/src", capability="filesystem.read"), root_files)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("classification", ["read", "write"])
@pytest.mark.parametrize("file_grant", [False, True])
async def test_shell_grants_use_the_computer_not_file_scopes(tmp_path, legacy, classification, file_grant) -> None:
    session = _session(user_id=uuid4(), current_directory="/work")
    root_files = _root_files(tmp_path, session)
    agent = ExecutionAgent(
        id=uuid4(), user_id=session.user_id, home_directory="/home/user",
        home_filesystem_policy="ask", external_filesystem_policy="ask",
        home_shell_enabled=True, external_shell_enabled=True,
    )
    request, = shell_command_permissions(
        agent.id, "/work", "cat /opt/input > /srv/output" if classification == "write" else "cat /opt/input /srv/output",
        ShellInspectResult(
            required_categories=[classification],
            targets=[{"path": "/opt/input", "kind": "input", "access": "read"},
                     {"path": "/srv/output", "kind": "path", "access": classification}],
            classification=classification, read_only=classification == "read", analysis_complete=True,
            commands=[], inspection_revision="a" * 64,
        ),
    )
    grants = [
        PermissionGrant(
            session_id=session.id, capability=f"{'resource' if legacy else 'category'}.{access}",
            resource_type="execution_path" if legacy or file_grant else "shell_computer",
            resource_scope=execution_directory_scope(agent.id, path, recursive=True) if legacy or file_grant else str(agent.id),
        )
        for path, access in (("/work", classification), ("/opt", "read"), ("/srv", classification))
    ]
    db = _db_with_grants(*grants)
    db.scalar.return_value = agent

    assert await PermissionPolicy.evaluate(
        db, session, request, root_files, ToolExecutionPolicy.ASK
    ) == (PermissionDecision.ASK if legacy or file_grant else PermissionDecision.ALLOW)
    db.scalars.return_value.all.return_value = []
    assert await PermissionPolicy.evaluate(
        db, session, request, root_files, ToolExecutionPolicy.ASK
    ) == PermissionDecision.ASK
    outside = SessionGrantScope("read", "execution_path", execution_directory_scope(agent.id, "/optional/tool"))
    assert not PermissionPolicy.scope_matches(grants[1], outside, root_files)


@pytest.mark.parametrize("granted_category", ["read", "write", "delete"])
@pytest.mark.parametrize("requested_category", ["read", "write", "delete"])
def test_computer_resource_grants_include_read_in_write(tmp_path, granted_category, requested_category):
    session = _session()
    root_files = _root_files(tmp_path, session)
    computer = uuid4()
    granted = execution_directory_permission(computer, "/project", capability=f"filesystem.{granted_category}")
    request = execution_directory_permission(computer, "/project/sub/file", capability=f"filesystem.{requested_category}")
    assert PermissionPolicy.grant_matches(_grant(granted), request, root_files) == (
        granted_category == requested_category or (granted_category, requested_category) == ("write", "read")
    )


def test_computer_delete_grant_does_not_advertise_shell_access():
    request = execution_directory_permission(uuid4(), "/project", capability="filesystem.delete")
    assert session_grants_display(request) == ["Löschen · einschließlich Unterpfaden: /project"]


@pytest.mark.parametrize("granted", ["read", "write", "delete", "read_sensitive"])
@pytest.mark.parametrize("requested", ["read", "write", "delete", "read_sensitive"])
def test_integration_category_coverage_is_resource_bound(tmp_path, granted, requested):
    connection = uuid4()
    capabilities = {"read": "calendar.read", "write": "calendar.write", "delete": "calendar.delete", "read_sensitive": "secrets.access"}

    def request(category, resource="calendar:one"):
        return integration_operation_permission(
            capability=capabilities[category], integration_id=connection, binding_revision="{}",
            operation_key="operation", resource_key=resource, title="Zugriff?", summary="Zugriff prüfen.",
        )

    roots = _root_files(tmp_path, _session())
    grant = _grant(request(granted))
    assert [PermissionPolicy.grant_matches(grant, target, roots) for target in (
        request(requested), request(requested, "calendar:two"),
    )] == [granted == requested or (granted, requested) == ("write", "read"), False]


@pytest.mark.parametrize("path, parent_path", [
    ("/work/docs/file", "/work/docs"), ("/file", "/"), ("/", None),
    ("C:/Users/file", "C:/Users"), ("C:/file", "C:/"), ("C:/", None),
])
def test_computer_parent_option_stops_at_the_root(tmp_path, path, parent_path):
    computer = uuid4()
    request = execution_directory_permission(computer, path, capability="filesystem.write")
    options = session_grant_options(request)
    assert {key: json.loads(value.effective_session_grants[0].resource_scope)["path"]
            for key, value in options.items()} == ({"requested": path, "parent": parent_path} if parent_path else {"requested": path})
    assert {option["id"]: option["scopeDisplay"] for option in session_grant_options_payload(request)} == {
        key: [f'Pfad „{value.effective_session_grants[0].resource_display}“ einschließlich Unterpfaden']
        for key, value in options.items()
    }


@pytest.mark.parametrize("path, parent_path", [("docs/file", "docs"), ("file", "."), (".", None)])
def test_session_parent_option_keeps_the_storage_identity(tmp_path, path, parent_path):
    roots = _root_files(tmp_path, _session())
    options = session_grant_options(_path_request(roots, path, capability="session.files.write"))
    if parent_path is None:
        assert list(options) == ["requested"]
    else:
        grant = _grant(options["parent"])
        assert [PermissionPolicy.grant_matches(grant, target, roots) for target in (
            _path_request(roots, f"{parent_path}/sibling" if parent_path != "." else "sibling"),
            replace(_path_request(roots, path), resource_scope=_path_request(_root_files(tmp_path, _session()), path).resource_scope),
        )] == [True, False]


@pytest.mark.parametrize("root", ["workspace", "session"])
@pytest.mark.parametrize("kind", ["file", "directory", "missing", "create_directory", "write_file"])
async def test_exact_file_consent_does_not_cover_subtrees(tmp_path, monkeypatch, root, kind):
    roots = _root_files(tmp_path, _session())
    if root == "workspace":
        path = "/opt/halogen/compose.yaml"
        stat = AsyncMock()
        if kind in {"missing", "create_directory", "write_file"}:
            stat.side_effect = ExecutionClientError("file.fileNotFound")
        else:
            stat.return_value = WorkspaceStatResult(path=path, kind=kind, size=0, modified_at=0.0)
        monkeypatch.setattr(ExecutionClient, "request", stat)
        request = execution_directory_permission(uuid4(), path, capability="filesystem.write")
    else:
        path = "compose.yaml"
        if kind == "file":
            roots.write_file("session", path, "services: {}")
        elif kind == "directory":
            roots.create_directory("session", path)
        request = _path_request(roots, path, capability="session.files.write")
    if kind in {"create_directory", "write_file"}:
        request = replace(request, action_kind=kind)
    prepared = await prepare_file_grants(request, roots)
    recursive = kind in {"directory", "missing", "create_directory"}
    assert {key: (json.loads(option.effective_session_grants[0].resource_scope).get("recursive", False),
                  PermissionPolicy.grant_matches(_grant(option), request, roots))
            for key, option in session_grant_options(prepared).items()} == {
        "requested": (recursive, recursive), "parent": (True, True),
    }


@pytest.mark.parametrize("scope", ["requested", "computer"])
def test_shell_scope_choice_controls_reuse(tmp_path, scope):
    computer = uuid4()

    def request(command="touch file", directory="/work", environment=None, agent=computer, category="write"):
        return shell_command_permissions(agent, directory, command, ShellInspectResult(
            required_categories=[category], classification=category, read_only=category == "read",
            targets=[], commands=[], analysis_complete=True, inspection_revision="a" * 64,
        ), environment=environment)[0]

    roots = _root_files(tmp_path, _session())
    grant = _grant(session_grant_options(request())[scope])
    option = next(option for option in session_grant_options_payload(request()) if option["id"] == scope)
    assert option["scopeDisplay"] == ["Diesen Befehl" if scope == "requested" else "Shell auf diesem Computer"]
    assert [PermissionPolicy.grant_matches(grant, target, roots) for target in (
        request(), request(category="read"), request(command="touch another"), request(directory="/other"),
        request(environment={"MODE": "test"}), request(agent=uuid4()),
    )] == [True, True, scope == "computer", scope == "computer", False, False]


def test_restricted_requests_have_no_broader_options():
    request = execution_directory_permission(uuid4(), "/work/file", capability="filesystem.write")
    assert [list(session_grant_options(restricted)) for restricted in (
        replace(request, sensitive_access=True), replace(request, available_decisions=("allow_once", "deny")),
        administrator_permission(uuid4(), "/work", capability="filesystem.write", path="/etc/file"),
    )] == [["requested"]] * 3


@pytest.mark.parametrize("category,label", [("write", "Lesen und Schreiben"), ("delete", "Löschen")])
def test_administrator_shell_grant_display_names_its_category(category, label):
    request = administrator_permission(uuid4(), "/work", capability=f"shell.{category}", command="touch new; rm old")
    assert session_grants_display(request) == [f"{label} · Diesen Shell-Befehl als Administrator ausführen: touch new; rm old · Arbeitsverzeichnis: /work"]
    assert session_grant_options_payload(request)[0]["scopeDisplay"] == ["Diesen Befehl"]


@pytest.mark.parametrize("legacy", [False, True])
def test_write_grants_never_include_deletion(tmp_path, legacy):
    roots = _root_files(tmp_path, _session())
    write = _path_request(roots, "file", capability="filesystem.write")
    grant = _grant(write)
    if legacy:
        grant.capability, grant.resource_scope = "resource.write", write.resource_scope
    assert PermissionPolicy.grant_matches(
        grant, _path_request(roots, "file", capability="filesystem.delete"), roots,
    ) is False


@pytest.mark.parametrize(
    ("path", "home_policy", "computer_external_policy", "profile_external_policy", "expected"),
    [
        ("/home/user/project", "auto", "deny", ToolExecutionPolicy.ASK, PermissionDecision.ASK),
        ("/home/user-other", "auto", "deny", ToolExecutionPolicy.AUTO, PermissionDecision.DENY),
        ("/srv/project", "auto", "ask", ToolExecutionPolicy.ASK, PermissionDecision.ASK),
        ("/srv/project", "auto", "ask", ToolExecutionPolicy.AUTO, PermissionDecision.ALLOW),
        ("/srv/project", "auto", "auto", ToolExecutionPolicy.DISABLED, PermissionDecision.DENY),
    ],
)
async def test_execution_filesystem_policy_uses_home_boundary(
    tmp_path, path, home_policy, computer_external_policy, profile_external_policy, expected
) -> None:
    session = _session(user_id=uuid4())
    root_files = _root_files(tmp_path, session)
    agent = ExecutionAgent(
        id=uuid4(),
        user_id=session.user_id,
        alias="Desktop",
        home_directory="/home/user",
        home_filesystem_policy=home_policy,
        external_filesystem_policy=computer_external_policy,
    )
    db = _db_with_grants()
    db.scalar.return_value = agent
    request = execution_directory_permission(
        agent.id, path, capability="filesystem.read"
    )

    assert await PermissionPolicy.evaluate(
        db,
        session,
        request,
        root_files,
        ToolExecutionPolicy.AUTO,
        external_working_directory_policy=profile_external_policy,
    ) == expected


async def test_current_directory_is_the_auto_workspace_boundary(tmp_path) -> None:
    session = _session(user_id=uuid4(), current_directory="/srv/project")
    root_files = _root_files(tmp_path, session)
    agent = ExecutionAgent(
        id=uuid4(),
        user_id=session.user_id,
        alias="Desktop",
        home_directory="/home/user",
        home_filesystem_policy="auto",
        external_filesystem_policy="ask",
    )
    db = _db_with_grants()
    db.scalar.return_value = agent

    inside = execution_directory_permission(
        agent.id, "/srv/project/src/app.py", capability="filesystem.write"
    )
    outside = execution_directory_permission(
        agent.id, "/home/user/notes.txt", capability="filesystem.write"
    )

    assert await PermissionPolicy.evaluate(
        db, session, inside, root_files, ToolExecutionPolicy.AUTO
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        db, session, outside, root_files, ToolExecutionPolicy.AUTO
    ) == PermissionDecision.ASK
    assert await PermissionPolicy.evaluate(
        db,
        session,
        outside,
        root_files,
        ToolExecutionPolicy.AUTO,
        external_working_directory_policy=ToolExecutionPolicy.AUTO,
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        db,
        session,
        outside,
        root_files,
        ToolExecutionPolicy.AUTO,
        external_working_directory_policy=ToolExecutionPolicy.DISABLED,
    ) == PermissionDecision.DENY


@pytest.mark.parametrize("capability", ["filesystem.read", "filesystem.write", "filesystem.delete"])
@pytest.mark.parametrize("temporary_directory", ["/tmp/mato-loop", "C:/Users/Test/AppData/Local/Temp/mato-loop"])
@pytest.mark.parametrize("case,expected", [
    ("temporary", PermissionDecision.ALLOW),
    ("outside", PermissionDecision.DENY),
    ("prefix", PermissionDecision.DENY),
    ("other_session", PermissionDecision.DENY),
    ("disabled", PermissionDecision.DENY),
    ("other_computer", PermissionDecision.DENY),
    ("hard_deny", PermissionDecision.DENY),
    ("sensitive", PermissionDecision.ASK),
])
async def test_profile_temporary_grant_is_bound_to_selected_computer_and_path(
    tmp_path, monkeypatch, capability, temporary_directory, case, expected,
):
    workspace = "C:/work" if temporary_directory.startswith("C:") else "/work"
    session = _session(user_id=uuid4(), preferred_execution_agent_id=uuid4(), current_directory=workspace)
    agent = ExecutionAgent(id=session.preferred_execution_agent_id, user_id=session.user_id,
                           home_directory=workspace, home_filesystem_policy="auto",
                           external_filesystem_policy="deny" if case == "hard_deny" else "ask")
    monkeypatch.setattr(execution_agent_connections, "system_info", lambda _id: SystemInfo(temporary_directory=temporary_directory))
    own = f"{temporary_directory}/{session.id}"
    path = {"outside": f"{workspace}/source.py", "prefix": f"{own}-other/file", "other_session": f"{temporary_directory}/{uuid4()}/file", "sensitive": f"{own}/.env"}.get(case, f"{own}/check.py")
    if capability == "filesystem.delete":
        expected = PermissionDecision.DENY
    elif case == "disabled" and capability == "filesystem.read":
        expected = PermissionDecision.ALLOW
    if case == "other_computer":
        session.preferred_execution_agent_id = uuid4()
    db = _db_with_grants()
    db.scalar.return_value = agent
    request = execution_directory_permission(agent.id, path, capability=capability)

    assert await PermissionPolicy.evaluate(
        db, session, request, _root_files(tmp_path, session), ToolExecutionPolicy.DISABLED,
        external_working_directory_policy=ToolExecutionPolicy.DISABLED,
        temporary_files_enabled=case != "disabled",
    ) == expected


async def test_shell_ignores_workspace_policy_but_keeps_sensitive_checks(
    tmp_path,
) -> None:
    session = _session(user_id=uuid4(), current_directory="/work")
    root_files = _root_files(tmp_path, session)
    agent = ExecutionAgent(
        id=uuid4(),
        user_id=session.user_id,
        home_directory="/home/user",
        home_filesystem_policy="auto",
        external_filesystem_policy="ask",
        home_shell_enabled=True,
        external_shell_enabled=True,
    )
    db = _db_with_grants()
    db.scalar.return_value = agent
    request = PermissionRequest(
        capability="shell.read",
        resource_type="shell_command",
        resource_scope=shell_command_scope(
            agent.id,
            "/work",
            "read",
            [["cat"]],
            ["/srv/config.yaml"],
            True,
            ["read"],
        ),
        action_kind="shell_run",
        risk_level="low",
        title="Lesen?",
        summary="Test",
    )

    assert await PermissionPolicy.evaluate(
        db,
        session,
        request,
        root_files,
        ToolExecutionPolicy.AUTO,
        external_working_directory_policy=ToolExecutionPolicy.ASK,
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        db,
        session,
        request,
        root_files,
        ToolExecutionPolicy.AUTO,
        external_working_directory_policy=ToolExecutionPolicy.AUTO,
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        db,
        session,
        replace(request, sensitive_access=True),
        root_files,
        ToolExecutionPolicy.AUTO,
        external_working_directory_policy=ToolExecutionPolicy.DISABLED,
    ) == PermissionDecision.ASK


@pytest.mark.parametrize("sensitive_policy", list(ToolExecutionPolicy))
@pytest.mark.parametrize("kind", ["file", "shell", "administrator"])
async def test_confidential_workspace_paths_honor_sensitive_policy(
    tmp_path, sensitive_policy, kind,
) -> None:
    session = _session(user_id=uuid4(), current_directory="/home/user/project")
    root_files = _root_files(tmp_path, session)
    agent = ExecutionAgent(
        id=uuid4(),
        user_id=session.user_id,
        alias="Desktop",
        home_directory="/home/user",
        home_filesystem_policy="auto",
        external_filesystem_policy="ask",
    )
    request = execution_directory_permission(
        agent.id, "/home/user/project/.env", capability="filesystem.read"
    )
    if kind == "shell":
        request, = shell_command_permissions(agent.id, session.current_directory, "cat .env", ShellInspectResult(
            required_categories=["read"],
            classification="read", read_only=True, analysis_complete=True, inspection_revision="a" * 64,
            commands=[],
            targets=[{"path": "/home/user/project/.env", "kind": "path", "access": "read"}],
        ))
        agent.home_shell_enabled = True
    elif kind == "administrator":
        session.preferred_execution_agent_id = agent.id
        request = administrator_permission(
            agent.id, session.current_directory, capability="filesystem.read", path="/home/user/project/.env",
        )
    grant = _grant(request, session.id)
    db = _db_with_grants(grant)
    db.scalar.return_value = agent

    assert await PermissionPolicy.evaluate(
        db, session, request, root_files, ToolExecutionPolicy.AUTO,
        sensitive_data_policy=sensitive_policy,
    ) == {
        ToolExecutionPolicy.AUTO: PermissionDecision.ALLOW,
        ToolExecutionPolicy.ASK: PermissionDecision.ASK,
        ToolExecutionPolicy.DISABLED: PermissionDecision.DENY,
    }[sensitive_policy]
    outside = execution_directory_permission(
        agent.id, "/home/user/.env", capability="filesystem.read"
    )
    assert await PermissionPolicy.evaluate(
        db,
        session,
        outside,
        root_files,
        ToolExecutionPolicy.AUTO,
        external_working_directory_policy=ToolExecutionPolicy.DISABLED,
        sensitive_data_policy=sensitive_policy,
    ) == PermissionDecision.DENY


@pytest.mark.parametrize("trust_level", list(TrustLevel))
async def test_file_provide_is_separate_auto_capability_for_confidential_workspace(
    tmp_path, trust_level
) -> None:
    session = _session(user_id=uuid4(), current_directory="/home/user/project")
    root_files = _root_files(tmp_path, session)
    agent = ExecutionAgent(
        id=uuid4(), user_id=session.user_id, alias="Desktop",
        home_directory="/home/user", home_filesystem_policy="auto",
        external_filesystem_policy="deny",
    )
    db = _db_with_grants()
    db.scalar.return_value = agent
    request = execution_directory_permission(
        agent.id, "/home/user/project/.env", capability="file.provide"
    )

    assert request.available_decisions == ("allow_once", "allow_session", "deny")
    assert await PermissionPolicy.evaluate(
        db, session, request, root_files, ToolExecutionPolicy.AUTO,
        trust_level=trust_level,
    ) == PermissionDecision.ALLOW
    agent.home_filesystem_policy = "deny"
    assert await PermissionPolicy.evaluate(
        db, session, request, root_files, ToolExecutionPolicy.AUTO,
        trust_level=trust_level,
    ) == PermissionDecision.DENY


def test_file_provide_defaults_and_saved_overrides_ignore_read_policy() -> None:
    settings = ToolSettings(trust_level=TrustLevel.PUBLIC)
    settings.permissions.policies["filesystem.read"] = ToolExecutionPolicy.DISABLED

    assert resolve_tool_execution_policy(
        settings, "file.provide", ToolExecutionPolicy.AUTO
    ).policy == ToolExecutionPolicy.AUTO
    settings.permissions.policies["file.provide"] = ToolExecutionPolicy.ASK
    assert resolve_tool_execution_policy(
        settings, "file.provide", ToolExecutionPolicy.AUTO
    ).policy == ToolExecutionPolicy.ASK
    settings.permissions.policies["file.provide"] = ToolExecutionPolicy.DISABLED
    assert resolve_tool_execution_policy(
        settings, "file.provide", ToolExecutionPolicy.AUTO
    ).policy == ToolExecutionPolicy.DISABLED


async def test_automation_scopes_are_canonical_current_session_and_revision_exact(
    tmp_path,
) -> None:
    session = _session(user_id=uuid4())
    root_files = _root_files(tmp_path, session)
    automation_id = uuid4()
    request = PermissionRequest(
        capability="automation.update",
        resource_type="automation",
        resource_scope=automation_scope(
            automation_id, 4, session.user_id, session.id
        ),
        action_kind="edit_automation",
        risk_level="medium",
        title="Automation ändern?",
        summary="Test",
    )
    grant = _grant(request, session.id)

    assert PermissionPolicy.grant_matches(grant, request, root_files)
    changed = replace(
        request,
        resource_scope=automation_scope(
            automation_id, 5, session.user_id, session.id
        ),
    )
    assert not PermissionPolicy.grant_matches(grant, changed, root_files)
    wrong_session = replace(
        request,
        resource_scope=automation_scope(
            automation_id, 4, session.user_id, uuid4()
        ),
    )
    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant),
        session,
        wrong_session,
        root_files,
        ToolExecutionPolicy.AUTO,
    ) == PermissionDecision.DENY
    malformed = replace(request, resource_scope='{"revision":4}')
    assert await PermissionPolicy.evaluate(
        _db_with_grants(),
        session,
        malformed,
        root_files,
        ToolExecutionPolicy.AUTO,
    ) == PermissionDecision.DENY
    collection = automation_collection_scope(session.user_id, session.id)
    assert collection == (
        f'{{"sessionId":"{session.id}","userId":"{session.user_id}"}}'
    )


@pytest.mark.parametrize("capability", ["automation.read", "automation.update"])
@pytest.mark.parametrize("scope, policy, expected", [
    ("current", ToolExecutionPolicy.ASK, PermissionDecision.ALLOW),
    ("other", ToolExecutionPolicy.ASK, PermissionDecision.ASK),
    ("foreign", ToolExecutionPolicy.AUTO, PermissionDecision.DENY),
    ("stale", ToolExecutionPolicy.AUTO, PermissionDecision.DENY),
    ("current", ToolExecutionPolicy.DISABLED, PermissionDecision.DENY),
])
async def test_session_automation_access_uses_persisted_owner_session_and_revision(tmp_path, capability, scope, policy, expected):
    session = _session(user_id=uuid4())
    automation = Automation(
        id=uuid4(), user_id=uuid4() if scope == "foreign" else session.user_id,
        execution_session_id=uuid4() if scope == "other" else session.id,
        revision=2 if scope == "stale" else 1,
    )
    db = _db_with_grants()
    db.get.return_value = automation
    request = PermissionRequest(
        capability, "automation", automation_scope(automation.id, 1, session.user_id, session.id),
        "get_automation" if capability == "automation.read" else "edit_automation", "medium", "Test", "Test",
    )
    assert await PermissionPolicy.evaluate(db, session, request, _root_files(tmp_path, session), policy) == expected


@pytest.mark.parametrize(
    ("root", "capability"),
    [
        ("session", "filesystem.read"),
        ("session", "filesystem.write"),
    ],
)
async def test_file_access_is_decided_only_for_current_host_bound_roots(
    tmp_path, root: str, capability: str
) -> None:
    session = _session()
    root_files = _root_files(tmp_path, session)
    db = _db_with_grants()

    assert await PermissionPolicy.evaluate(
        db,
        session,
        _path_request(root_files, "files/notes.txt", root=root, capability=capability),
        root_files,
    ) == PermissionDecision.ALLOW
    db.scalars.assert_not_awaited()


async def test_global_policy_can_require_or_deny_a_host_bound_file_action(tmp_path) -> None:
    session = _session()
    root_files = _root_files(tmp_path, session)
    request = _path_request(
        root_files,
        "files/notes.txt",
        capability="filesystem.delete",
    )

    assert await PermissionPolicy.evaluate(
        _db_with_grants(), session, request, root_files, ToolExecutionPolicy.ASK
    ) == PermissionDecision.ASK
    assert await PermissionPolicy.evaluate(
        _db_with_grants(), session, request, root_files, ToolExecutionPolicy.DISABLED
    ) == PermissionDecision.DENY


@pytest.mark.parametrize("capability", ["session.files.delete", "sensitive_data.access"])
async def test_policy_revocation_while_waiting_for_approval_stops_action(
    tmp_path, monkeypatch, capability,
) -> None:
    session = _session()
    root_files = _root_files(tmp_path, session)
    request = _path_request(
        root_files,
        "files/notes.txt",
        capability="filesystem.delete",
    )
    request = replace(request, sensitive_access=capability == "sensitive_data.access")
    settings_loads = 0

    async def load_settings(_db, settings_type, **_kwargs):
        nonlocal settings_loads
        if settings_type is IntegrationSettings:
            return IntegrationSettings()
        return ToolSettings()

    async def load_optional(_db, settings_type, **_kwargs):
        nonlocal settings_loads
        if settings_type is not AgentProfilesSettings:
            return None
        settings_loads += 1
        return _profile_settings(
            {
                "policies": {
                    "session.files.delete": "auto",
                    capability: (
                        "ask" if settings_loads == 1 else "disabled"
                    )
                }
            }
        )

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load_settings))
    monkeypatch.setattr(
        SettingsStore, "load_optional", AsyncMock(side_effect=load_optional)
    )
    monkeypatch.setattr(
        AgentRuntime, "_request_approval", AsyncMock(return_value="allow_once")
    )
    db = _db_with_grants()

    decision = await AgentRuntime._authorize(
        db,
        AgentRun(id=uuid4(), session_id=session.id),
        session,
        ToolCall(id="delete", name="delete_path", arguments={}),
        request,
        root_files,
    )

    assert decision == PermissionDecision.DENY


async def test_administrator_once_approval_is_reused_for_revalidation(tmp_path, monkeypatch):
    from mato_loop_backend.agent.run_contracts import ApprovalResolution

    agent = ExecutionAgent(id=uuid4(), user_id=uuid4(), home_directory="/home/user", home_filesystem_policy="auto", external_filesystem_policy="ask")
    session = _session(user_id=agent.user_id, preferred_execution_agent_id=agent.id, current_directory="/work")
    db = _db_with_grants()
    db.scalar.return_value = agent
    settings = _tool_settings({"policies": {"filesystem.write": "ask", "working_directory.external": "auto"}})
    monkeypatch.setattr(mock_agent_runtime, "permission_origin", AsyncMock(return_value=session))
    monkeypatch.setattr(mock_agent_runtime, "delegation_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(mock_agent_runtime, "load_agent_profile_tools", AsyncMock(return_value=(settings, sysadmin_profile())))
    monkeypatch.setattr(mock_agent_runtime, "load_effective_integrations", AsyncMock(return_value=IntegrationSettings()))
    approve = AsyncMock(return_value=ApprovalResolution("allow_once"))
    monkeypatch.setattr(AgentRuntime, "_request_approval", approve)
    request = administrator_permission(agent.id, "/work", capability="filesystem.write", path="/etc/demo.conf")
    run = AgentRun(id=uuid4(), session_id=session.id)
    call = ToolCall("edit", "edit_file", {"root": "computer", "path": "/etc/demo.conf", "oldText": "old", "newText": "new", "elevated": True})
    resolution = {}
    roots = _root_files(tmp_path, session)
    assert await AgentRuntime._authorize(db, run, session, call, request, roots, resolution) == PermissionDecision.ALLOW
    assert await AgentRuntime._authorize(db, run, session, call, request, roots, resolution, check_only=True) == PermissionDecision.ALLOW
    assert approve.await_count == 1
    resolution.clear()
    resolution["argument_edit_approved"] = json.dumps({
        "arguments": call.arguments, "permission": mock_agent_run_support._argument_edit_permission_binding(request),
    })
    assert await AgentRuntime._authorize(db, run, session, call, request, roots, resolution) == PermissionDecision.ALLOW
    assert await AgentRuntime._authorize(db, run, session, call, request, roots, resolution, check_only=True) == PermissionDecision.ALLOW
    assert approve.await_count == 1
    settings.permissions.policies["filesystem.write"] = ToolExecutionPolicy.DISABLED
    assert await AgentRuntime._authorize(db, run, session, call, request, roots, resolution, check_only=True) == PermissionDecision.DENY


@pytest.mark.parametrize("lifetime", ["run", "session", "exact"])
@pytest.mark.parametrize("kind", ["file", "shell", "administrator", "directory"])
async def test_runtime_grant_lifetime_and_boundaries(tmp_path, monkeypatch, lifetime, kind):
    session = _session(user_id=uuid4(), agent_profile_id=uuid4(), current_directory="/work")
    agent = ExecutionAgent(id=uuid4(), user_id=session.user_id, home_directory="/home/user",
        home_filesystem_policy="auto", external_filesystem_policy="auto",
        home_shell_enabled=True, external_shell_enabled=True)
    session.preferred_execution_agent_id = agent.id
    run = AgentRun(id=uuid4(), session_id=session.id, trust_level="public")
    roots = _root_files(tmp_path, session)
    inspection = ShellInspectResult(required_categories=["write"], targets=[], commands=[], read_only=False, classification="write",
        analysis_complete=True, inspection_revision="a" * 64)

    def requested(target):
        if kind == "file":
            result = _path_request(roots, target)
        elif kind == "shell":
            result, = shell_command_permissions(agent.id, "/work", f"touch {target}", inspection)
        elif kind == "directory":
            result = execution_directory_permission(agent.id, f"/work/{target}")
        else:
            result = administrator_permission(agent.id, "/work", capability="shell.write", command=f"touch {target}", inspection=inspection)
        return replace(result, sensitive_access=lifetime == "exact")

    request = requested("notes.txt")
    call = ToolCall("first", "read" if kind == "file" else "shell_run",
        {"path": "notes.txt"} if kind == "file" else {"command": "touch notes.txt"})
    if kind == "directory":
        call = ToolCall("first", "change_directory", {"path": "/work/notes.txt"})
    grant = _grant(request, session.id, run_id=None if lifetime == "session" else run.id,
                   agent_profile_id=session.agent_profile_id, trust_level="public")
    if kind == "directory":
        session.current_directory = "/work/notes.txt/child"
    if lifetime == "exact":
        grant.capability = request.capability
        grant.resource_type = "run_action"
        grant.resource_scope = mock_agent_run_support._run_action_scope(request, call)
    settings = _tool_settings({"policies": {request.capability: "ask", "working_directory.external": "auto"}})
    monkeypatch.setattr(mock_agent_runtime, "load_agent_profile_tools", AsyncMock(return_value=(settings, sysadmin_profile())))
    monkeypatch.setattr(mock_agent_runtime, "load_effective_integrations", AsyncMock(return_value=IntegrationSettings()))
    monkeypatch.setattr(AgentRuntime, "_request_approval", AsyncMock(return_value="deny"))
    engine = create_engine("sqlite://")
    PermissionGrant.__table__.create(engine)
    try:
        with Session(engine) as stored:
            stored.add(grant)
            stored.commit()
            db = AsyncMock(spec=AsyncSession)
            db.scalar.side_effect = lambda statement: agent if statement.column_descriptions[0]["entity"] is ExecutionAgent else stored.scalar(statement)
            db.scalars.side_effect = stored.scalars

            async def authorize(current_run=run, permission=request, tool_call=call):
                return await AgentRuntime._authorize(db, current_run, session, tool_call, permission, roots)

            outcomes = [
                await authorize(),
                await authorize(tool_call=ToolCall("next", call.name, {**call.arguments, "_reason": "Repeat access"})),
                await authorize(AgentRun(id=uuid4(), session_id=session.id, trust_level="public")),
                await authorize(permission=requested("other.txt")),
                await authorize(AgentRun(id=run.id, session_id=session.id, trust_level="private")),
                await authorize(AgentRun(id=run.id, session_id=session.id, trust_level="public", agent_profile_id=uuid4())),
            ]
            if lifetime == "exact":
                outcomes.extend([
                    await authorize(tool_call=ToolCall("changed", "read", {"path": "notes.txt", "offset": 10})),
                    await authorize(permission=replace(request, approval_details={"revision": "changed"})),
                ])
            settings.permissions.policies[request.capability] = ToolExecutionPolicy.DISABLED
            outcomes.append(await authorize())
            assert outcomes == [
                PermissionDecision.ALLOW, PermissionDecision.ALLOW,
                PermissionDecision.ALLOW if lifetime == "session" else PermissionDecision.USER_DENIED,
                PermissionDecision.USER_DENIED,
                PermissionDecision.USER_DENIED, PermissionDecision.USER_DENIED,
                *([PermissionDecision.USER_DENIED] * 2 if lifetime == "exact" else []),
                PermissionDecision.DENY,
            ]
    finally:
        engine.dispose()


@pytest.mark.parametrize("elevated", [False, True])
@pytest.mark.parametrize("command", ["cat input > output", "cat other > output"])
async def test_shell_write_grant_covers_read_only_for_its_whole_command(tmp_path, elevated, command):
    session = _session(user_id=uuid4(), current_directory="/work")
    agent = ExecutionAgent(id=uuid4(), user_id=session.user_id, home_directory="/home/user",
        home_filesystem_policy="auto", external_filesystem_policy="auto", home_shell_enabled=True, external_shell_enabled=True)
    session.preferred_execution_agent_id = agent.id
    inspection = ShellInspectResult(required_categories=["read", "write"], targets=[], commands=[],
        classification="write", analysis_complete=True, inspection_revision="a" * 64)
    requests = tuple(administrator_permission(agent.id, "/work", capability=f"shell.{category}",
        command=command, inspection=inspection) for category in inspection.required_categories) if elevated else shell_command_permissions(
        agent.id, "/work", command, inspection,
    )
    grant = PermissionGrant(session_id=session.id, capability="category.write", resource_type="shell_action",
        resource_scope=shell_action_scope(agent.id, "/work", "cat input > output", elevated=elevated))
    db = _db_with_grants(grant)
    db.scalar.return_value = agent
    roots = _root_files(tmp_path, session)
    assert [await PermissionPolicy.evaluate(db, session, request, roots, ToolExecutionPolicy.ASK)
            for request in requests] == [PermissionDecision.ALLOW if command == "cat input > output" else PermissionDecision.ASK] * 2


@pytest.mark.parametrize("kind", ["file", "changed_file", "shell"])
@pytest.mark.parametrize(
    ("policy_after_approval", "expected"),
    [
        ("ask", PermissionDecision.ALLOW),
        ("disabled", PermissionDecision.DENY),
    ],
)
async def test_file_workspace_or_shell_action_policy_is_revalidated_after_approval(
    tmp_path, monkeypatch, kind, policy_after_approval, expected
) -> None:
    session = _session(user_id=uuid4(), current_directory="/work")
    root_files = _root_files(tmp_path, session)
    agent = ExecutionAgent(
        id=uuid4(),
        user_id=session.user_id,
        home_directory="/home/user",
        home_filesystem_policy="auto",
        external_filesystem_policy="ask",
        home_shell_enabled=True,
        external_shell_enabled=True,
    )
    if kind != "shell":
        request = execution_directory_permission(
            agent.id, "/srv/data.txt", capability="filesystem.read"
        )
        call = ToolCall(id="read", name="read", arguments={})
        stat = WorkspaceStatResult(path="/srv/data.txt", kind="file", size=0, modified_at=0.0)
        monkeypatch.setattr(ExecutionClient, "request", AsyncMock(return_value=stat))
    else:
        request = PermissionRequest(
            capability="shell.read",
            resource_type="shell_command",
            resource_scope=shell_command_scope(
                agent.id,
                "/work",
                "read",
                [["cat"]],
                ["/srv/data.txt"],
                True,
                ["read"],
            ),
            action_kind="shell_run",
            risk_level="low",
            title="Lesen?",
            summary="Test",
        )
        call = ToolCall(id="shell", name="shell_run", arguments={})
    settings_loads = 0

    async def load_settings(_db, settings_type, **_kwargs):
        return IntegrationSettings() if settings_type is IntegrationSettings else ToolSettings()

    async def load_optional(_db, settings_type, **_kwargs):
        nonlocal settings_loads
        if settings_type is not AgentProfilesSettings:
            return None
        settings_loads += 1
        return _profile_settings({
            "policies": {
                request.capability: ("ask" if settings_loads == 1 else policy_after_approval) if kind == "shell" else "auto",
                "working_directory.external": (
                    "ask" if settings_loads == 1 else policy_after_approval
                ) if kind != "shell" else "disabled",
            }
        })

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load_settings))
    monkeypatch.setattr(
        SettingsStore, "load_optional", AsyncMock(side_effect=load_optional)
    )

    async def approve_file(_db, _run, _session, _call, permission, **_kwargs):
        if kind != "shell":
            db.scalars.return_value.all.return_value = [_grant(permission, session.id)]
            if kind == "changed_file":
                stat.kind = "directory"
        return "allow_once"

    approve = AsyncMock(side_effect=approve_file)
    monkeypatch.setattr(AgentRuntime, "_request_approval", approve)
    db = _db_with_grants()
    db.scalar.return_value = agent

    decision = await AgentRuntime._authorize(
        db,
        AgentRun(id=uuid4(), session_id=session.id, trust_level="public"),
        session,
        call,
        request,
        root_files,
    )

    assert decision == (PermissionDecision.DENY if kind == "changed_file" else expected)
    approve.assert_awaited_once()


async def test_skill_policy_revocation_while_waiting_stops_loading(
    tmp_path, monkeypatch
) -> None:
    session = _session(user_id=uuid4())
    request = skill_load_permission(session.user_id, "Recherche")
    settings_loads = 0

    async def load_settings(_db, settings_type, **_kwargs):
        nonlocal settings_loads
        if settings_type is IntegrationSettings:
            return IntegrationSettings()
        return ToolSettings()

    async def load_optional(_db, settings_type, **_kwargs):
        nonlocal settings_loads
        if settings_type is not AgentProfilesSettings:
            return None
        settings_loads += 1
        return _profile_settings(
            {
                "policies": {
                    "skills.load": "ask" if settings_loads == 1 else "disabled"
                }
            }
        )

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load_settings))
    monkeypatch.setattr(
        SettingsStore, "load_optional", AsyncMock(side_effect=load_optional)
    )
    monkeypatch.setattr(
        AgentRuntime, "_request_approval", AsyncMock(return_value="allow_once")
    )

    decision = await AgentRuntime._authorize(
        _db_with_grants(),
        AgentRun(id=uuid4(), session_id=session.id),
        session,
        ToolCall(id="load", name="load_skill", arguments={}),
        request,
        _root_files(tmp_path, session),
    )

    assert decision == PermissionDecision.DENY


@pytest.mark.usefixtures("mock_db_settings")
async def test_runtime_uses_bound_integration_resource_policy(
    tmp_path, monkeypatch
) -> None:
    session = _session()
    integration_id = uuid4()
    integration = IntegrationInstance(
        id=integration_id,
        name="Kalender",
        integration_type="search",
        adapter_type="searxng",
        config={"base_url": "https://search.example.com"},
        permissions={"publicPermissions": {"resourcePolicies": {
            "calendar:work": {"network.search": "auto"},
        }}},
    )
    request = PermissionRequest(
        capability="network.search",
        resource_type="network_service",
        resource_scope="calendar-search",
        action_kind="calendar_search",
        risk_level="medium",
        title="Kalender durchsuchen?",
        summary="Der Agent möchte einen Kalender durchsuchen.",
        policy_integration_id=integration_id,
        policy_resource_key="calendar:work",
    )
    tools = _tool_settings({"policies": {"network.search": "ask"}})

    async def load_settings(_db, settings_type, **_kwargs):
        return (
            IntegrationSettings(instances=[integration])
            if settings_type is IntegrationSettings
            else tools
        )

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load_settings))
    monkeypatch.setattr(
        tool_registry,
        "integration_resource_actions",
        Mock(
            return_value=(
                ToolAction(
                    "network.search",
                    "Suchen",
                    category="read",
                ),
            )
        ),
    )
    request_approval = AsyncMock(side_effect=AssertionError("Approval must not be requested"))
    monkeypatch.setattr(AgentRuntime, "_request_approval", request_approval)

    decision = await AgentRuntime._authorize(
        _db_with_grants(),
        AgentRun(id=uuid4(), session_id=session.id),
        session,
        ToolCall(id="search", name="calendar_search", arguments={}),
        request,
        _root_files(tmp_path, session),
    )

    assert decision == PermissionDecision.ALLOW
    request_approval.assert_not_awaited()


@pytest.mark.parametrize(
    ("global_policy", "expected", "approval_count"),
    (
        (None, PermissionDecision.ALLOW, 1),
        ("disabled", PermissionDecision.DENY, 0),
    ),
)
async def test_runtime_discovery_uses_integration_baseline_not_resource_policy(
    tmp_path, monkeypatch, global_policy, expected, approval_count
) -> None:
    session = _session()
    integration = IntegrationInstance(
        name="Kalender",
        integration_type="calendar",
        adapter_type="caldav",
        config={
            "base_url": "https://calendar.example.com",
            "username": "user",
        },
    )
    request = integration_operation_permission(
        capability="calendar.read",
        integration_id=integration.id,
        binding_revision="{}",
        operation_key="list-calendars",
        title="Kalender auflisten?",
        summary="Der Agent möchte Kalender auflisten.",
        use_integration_policy=False,
    )
    settings_data = {
        "integrationPolicies": {
            str(integration.id): {"calendar.read": "disabled"}
        }
    }
    if global_policy is not None:
        settings_data["policies"] = {"calendar.read": global_policy}
    tools = ToolSettings()
    integration.permissions.public_permissions.resource_policies = {"calendar:work": {"calendar.read": "disabled"}}
    if global_policy is not None:
        integration.permissions.public_permissions.policies["calendar.read"] = global_policy
    integrations = IntegrationSettings(instances=[integration, *(
        IntegrationInstance(
            name="Browser", integration_type="browser", adapter_type="browser",
            execution_agent_id=uuid4(),
        )
        for _ in range(2)
    )])

    async def load_settings(_db, settings_type, **_kwargs):
        return integrations if settings_type is IntegrationSettings else tools

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load_settings))
    monkeypatch.setattr(
        SettingsStore,
        "load_optional",
        AsyncMock(
            side_effect=lambda _db, settings_type, **_kwargs: (
                _profile_settings(settings_data)
                if settings_type is AgentProfilesSettings
                else None
            )
        ),
    )
    request_approval = AsyncMock(return_value="allow_once")
    monkeypatch.setattr(AgentRuntime, "_request_approval", request_approval)

    decision = await AgentRuntime._authorize(
        _db_with_grants(),
        AgentRun(id=uuid4(), session_id=session.id),
        session,
        ToolCall(id="list", name="calendar_list_calendars", arguments={}),
        request,
        _root_files(tmp_path, session),
    )

    assert request.policy_integration_id == integration.id
    assert decision == expected
    assert request_approval.await_count == approval_count


@pytest.mark.usefixtures("mock_db_settings")
async def test_runtime_ignores_override_after_resource_contract_removal(
    tmp_path, monkeypatch
) -> None:
    session = _session()
    integration = IntegrationInstance(
        name="Kalender",
        integration_type="search",
        adapter_type="searxng",
        config={"base_url": "https://search.example.com"},
    )
    request = PermissionRequest(
        capability="network.search",
        resource_type="network_service",
        resource_scope="calendar-search",
        action_kind="calendar_search",
        risk_level="medium",
        title="Kalender durchsuchen?",
        summary="Der Agent möchte einen Kalender durchsuchen.",
        policy_integration_id=integration.id,
        policy_resource_key="calendar:work",
    )
    tools = _tool_settings(
        {
            "policies": {"network.search": "ask"},
            "resourcePolicies": {
                str(integration.id): {
                    "calendar:work": {"network.search": "auto"}
                }
            },
        }
    )

    async def load_settings(_db, settings_type, **_kwargs):
        return (
            IntegrationSettings(instances=[integration])
            if settings_type is IntegrationSettings
            else tools
        )

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load_settings))
    monkeypatch.setattr(
        AgentRuntime, "_request_approval", AsyncMock(return_value="deny")
    )

    decision = await AgentRuntime._authorize(
        _db_with_grants(),
        AgentRun(id=uuid4(), session_id=session.id),
        session,
        ToolCall(id="search", name="calendar_search", arguments={}),
        request,
        _root_files(tmp_path, session),
    )

    assert decision == PermissionDecision.DENY
    AgentRuntime._request_approval.assert_not_awaited()


def test_integration_policy_precedence_and_hard_limits() -> None:
    integration_id = uuid4()
    action = ToolAction(
        "tasks.read",
        "Aufgaben lesen",
        category="read",
    )
    settings = _tool_settings(
        {
            "policies": {action.capability: "ask"},
            "integrationPolicies": {
                str(integration_id): {action.capability: "auto"}
            },
            "resourcePolicies": {
                str(integration_id): {
                    "project:main": {action.capability: "ask"}
                }
            },
        }
    )

    integration = resolve_tool_execution_policy(
        settings,
        action.capability,
        action.default_policy(settings),
        integration_id=integration_id,
    )
    resource = resolve_tool_execution_policy(
        settings,
        action.capability,
        action.default_policy(settings),
        integration_id=integration_id,
        resource_key="project:main",
    )
    hard_limit = resolve_tool_execution_policy(
        settings,
        action.capability,
        action.default_policy(settings),
        integration_id=integration_id,
        resource_key="project:main",
        hard_unavailable_capabilities=frozenset({action.capability}),
    )

    assert (integration.policy, integration.source) == (
        ToolExecutionPolicy.AUTO,
        "integration",
    )
    assert (resource.policy, resource.source) == (
        ToolExecutionPolicy.ASK,
        "resource",
    )
    assert (hard_limit.policy, hard_limit.reason) == (
        ToolExecutionPolicy.DISABLED,
        "hard_unavailable",
    )

    settings.permissions.policies[action.capability] = ToolExecutionPolicy.DISABLED
    overridden_disabled = resolve_tool_execution_policy(
        settings,
        action.capability,
        action.default_policy(settings),
        integration_id=integration_id,
        resource_key="project:main",
    )
    assert (overridden_disabled.policy, overridden_disabled.source) == (
        ToolExecutionPolicy.ASK,
        "resource",
    )

    restricted = ToolAction(
        "tasks.delete",
        "Aufgaben löschen",
        category="delete",
    )
    settings.permissions.policies[restricted.capability] = ToolExecutionPolicy.AUTO
    automatic = resolve_tool_execution_policy(
        settings,
        restricted.capability,
        restricted.default_policy(settings),
    )
    assert (automatic.policy, automatic.reason) == (
        ToolExecutionPolicy.AUTO,
        "configured",
    )

    settings.permissions.policies[action.capability] = ToolExecutionPolicy.ASK
    settings.permissions.integration_policies[str(integration_id)] = {
        action.capability: "invalid"  # type: ignore[dict-item]
    }
    invalid = resolve_tool_execution_policy(
        settings,
        action.capability,
        action.default_policy(settings),
        integration_id=integration_id,
        resource_key="project:main",
    )
    assert (invalid.policy, invalid.reason) == (
        ToolExecutionPolicy.DISABLED,
        "invalid_policy",
    )


def test_integration_grants_match_stable_resource_not_operation(tmp_path) -> None:
    session = _session()
    root_files = _root_files(tmp_path, session)
    integration_id = uuid4()
    revision = '{"adapter_type":"synthetic","enabled":true}'

    def request(
        *,
        selected_id=integration_id,
        selected_revision=revision,
        resource_key="project:main",
        operation_key="read:8d7f",
    ):
        return integration_operation_permission(
            capability="tasks.read",
            integration_id=selected_id,
            binding_revision=selected_revision,
            resource_key=resource_key,
            operation_key=operation_key,
            title="Aufgaben lesen?",
            summary="Der Agent möchte Aufgaben lesen.",
        )

    granted = request()
    grant = _grant(granted, session.id)

    assert PermissionPolicy.grant_matches(grant, granted, root_files)
    assert not PermissionPolicy.grant_matches(
        grant, request(selected_id=uuid4()), root_files
    )
    assert not PermissionPolicy.grant_matches(
        grant,
        request(selected_revision='{"adapter_type":"other","enabled":true}'),
        root_files,
    )
    assert not PermissionPolicy.grant_matches(
        grant, request(resource_key="project:other"), root_files
    )
    assert PermissionPolicy.grant_matches(
        grant, request(operation_key="read:other"), root_files
    )


async def test_stale_or_missing_workspace_cannot_be_approved(tmp_path) -> None:
    session = _session()
    root_files = _root_files(tmp_path, session)

    with pytest.raises(ValueError, match="No working directory is selected"):
        _path_request(root_files, "notes.txt", root="workspace")


async def test_root_scope_from_another_session_is_denied(tmp_path) -> None:
    session = _session()
    current = _root_files(tmp_path, session)
    other = _session()
    other_files = _root_files(tmp_path, other)

    assert await PermissionPolicy.evaluate(
        _db_with_grants(),
        session,
        _path_request(other_files, "files/notes.txt"),
        current,
    ) == PermissionDecision.DENY


async def test_legacy_global_path_permission_is_hard_denied(tmp_path) -> None:
    session = _session()
    root_files = _root_files(tmp_path, session)
    request = PermissionRequest(
        capability="filesystem.read",
        resource_type="path",
        resource_scope="notes.txt",
        action_kind="read_file",
        risk_level="medium",
        title="Datei lesen?",
        summary="Legacy-Pfad",
    )

    assert await PermissionPolicy.evaluate(
        _db_with_grants(), session, request, root_files
    ) == PermissionDecision.DENY


async def test_current_session_read_honors_automatic_policy(tmp_path) -> None:
    session = _session()
    db = _db_with_grants()

    decision = await PermissionPolicy.evaluate(
        db,
        session,
        session_permission(session.id),
        _root_files(tmp_path, session),
        ToolExecutionPolicy.AUTO,
    )

    assert decision == PermissionDecision.ALLOW
    db.scalars.assert_not_awaited()


@pytest.mark.parametrize(("policy", "granted", "expected"), [
    (ToolExecutionPolicy.AUTO, False, PermissionDecision.ALLOW),
    (ToolExecutionPolicy.ASK, False, PermissionDecision.ASK),
    (ToolExecutionPolicy.ASK, True, PermissionDecision.ALLOW),
    (ToolExecutionPolicy.DISABLED, True, PermissionDecision.DENY),
])
async def test_computer_list_and_select_permissions_are_owner_scoped(tmp_path, policy, granted, expected) -> None:
    session = _session(user_id=uuid4())
    roots = _root_files(tmp_path, session)
    agent = ExecutionAgent(id=uuid4(), user_id=session.user_id, alias="Server")
    db = _db_with_grants()
    db.scalar.return_value = agent

    assert await PermissionPolicy.evaluate(
        db, session, execution_agents_list_permission(session.user_id), roots,
        ToolExecutionPolicy.AUTO,
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        db, session, execution_agents_list_permission(uuid4()), roots,
        ToolExecutionPolicy.AUTO,
    ) == PermissionDecision.DENY
    request = execution_agent_select_permission(agent.id, agent.alias)
    if granted:
        db.scalars.return_value.all.return_value = [_grant(request, session.id)]
    assert await PermissionPolicy.evaluate(
        db, session, request, roots, policy,
    ) == expected
    agent.user_id = uuid4()
    assert await PermissionPolicy.evaluate(
        db, session, request, roots, ToolExecutionPolicy.ASK,
    ) == PermissionDecision.DENY


@pytest.mark.parametrize("sensitive_policy", list(ToolExecutionPolicy))
@pytest.mark.parametrize("ceiling_policy", [None, ToolExecutionPolicy.ASK, ToolExecutionPolicy.DISABLED])
async def test_runtime_resolves_sensitive_policy_without_overriding_action_policy(tmp_path, monkeypatch, sensitive_policy, ceiling_policy):
    agent = ExecutionAgent(id=uuid4(), user_id=uuid4(), home_directory="/work", home_filesystem_policy="auto")
    session = _session(user_id=agent.user_id, current_directory="/work")
    settings = _tool_settings({"policies": {"filesystem.read": "auto", "sensitive_data.access": sensitive_policy}})
    monkeypatch.setattr(mock_agent_runtime, "permission_origin", AsyncMock(return_value=session))
    ceiling = None
    if ceiling_policy is not None:
        parent_settings = settings.model_copy(deep=True)
        parent_settings.permissions.policies["sensitive_data.access"] = ceiling_policy
        ceiling = permission_snapshot(parent_settings, {"read"})
        sensitive_policy = min((sensitive_policy, ceiling_policy), key=list(ToolExecutionPolicy).index)
    monkeypatch.setattr(mock_agent_runtime, "delegation_snapshot", AsyncMock(return_value=ceiling))
    monkeypatch.setattr(mock_agent_runtime, "load_agent_profile_tools", AsyncMock(return_value=(settings, sysadmin_profile())))
    monkeypatch.setattr(mock_agent_runtime, "load_effective_integrations", AsyncMock(return_value=IntegrationSettings()))
    approve = AsyncMock(return_value="deny")
    monkeypatch.setattr(AgentRuntime, "_request_approval", approve)
    db = _db_with_grants()
    db.scalar.return_value = agent
    decision = await AgentRuntime._authorize(
        db, AgentRun(id=uuid4(), session_id=session.id, trust_level="public"), session,
        ToolCall("read", "read", {"root": "computer", "path": "/work/.env"}),
        execution_directory_permission(agent.id, "/work/.env", capability="filesystem.read"),
        _root_files(tmp_path, session),
    )
    assert (decision, approve.await_count) == {
        ToolExecutionPolicy.AUTO: (PermissionDecision.ALLOW, 0),
        ToolExecutionPolicy.ASK: (PermissionDecision.USER_DENIED, 1),
        ToolExecutionPolicy.DISABLED: (PermissionDecision.DENY, 0),
    }[sensitive_policy]


@pytest.mark.parametrize("kind", ["url", "root", "execution", "shell"])
async def test_child_uses_parent_grants_and_revocation_stops_execution(tmp_path, kind):
    parent = _session(user_id=uuid4())
    child = _session(user_id=parent.user_id, parent_session_id=parent.id, current_directory="/work")
    roots = _root_files(tmp_path, child)
    agent = ExecutionAgent(id=uuid4(), user_id=parent.user_id, home_directory="/home",
                           home_filesystem_policy="ask", external_filesystem_policy="ask",
                           home_shell_enabled=True, external_shell_enabled=True)
    request = {
        "url": webfetch_permission("https://example.org"),
        "root": _path_request(roots, "note.txt"),
        "execution": execution_directory_permission(agent.id, "/work", capability="filesystem.read"),
        "shell": PermissionRequest(capability="shell.read", resource_type="shell_command",
            resource_scope=shell_command_scope(agent.id, "/work", "read", [["pwd"]], [], True,
                action=shell_action_scope(agent.id, "/work", "pwd")),
            action_kind="shell", risk_level="low", title="Read", summary="Read",
            session_grants=(SessionGrantScope("read", "shell_action", shell_action_scope(agent.id, "/work", "pwd")),)),
    }[kind]
    grant = _grant(request, parent.id)
    db = _db_with_grants(grant)
    db.get.return_value = parent
    db.scalar.return_value = agent
    assert await PermissionPolicy.evaluate(
        db, child, request, roots, ToolExecutionPolicy.ASK, permission_session_id=parent.id
    ) == PermissionDecision.ALLOW
    assert parent.id in db.scalars.await_args.args[0].compile().params.values()
    db.scalars.return_value.all.return_value = []
    assert await PermissionPolicy.evaluate(
        db, child, request, roots, ToolExecutionPolicy.ASK, permission_session_id=parent.id
    ) == PermissionDecision.ASK
    if kind == "root":
        assert await PermissionPolicy.evaluate(
            db, child, request, _root_files(tmp_path, parent), ToolExecutionPolicy.AUTO,
            permission_session_id=parent.id,
        ) == PermissionDecision.DENY


@pytest.mark.parametrize("invalid", ["unrelated", "owner", "missing"])
async def test_child_cannot_choose_another_permission_namespace(tmp_path, invalid):
    parent = _session(user_id=uuid4())
    child = _session(user_id=parent.user_id, parent_session_id=parent.id)
    if invalid == "unrelated":
        child.parent_session_id = uuid4()
    elif invalid == "owner":
        parent.user_id = uuid4()
    db = _db_with_grants()
    db.get.return_value = None if invalid == "missing" else parent
    assert await PermissionPolicy.evaluate(
        db, child, webfetch_permission("https://example.org"), _root_files(tmp_path, child),
        ToolExecutionPolicy.AUTO, permission_session_id=parent.id,
    ) == PermissionDecision.DENY
    db.scalars.assert_not_awaited()


async def test_parent_can_read_only_its_own_child_sessions(tmp_path):
    parent = _session(user_id=uuid4())
    child = _session(user_id=parent.user_id, parent_session_id=parent.id)
    db = _db_with_grants()
    db.get.return_value = child
    roots = _root_files(tmp_path, parent)
    assert await PermissionPolicy.evaluate(db, parent, session_permission(child.id), roots, ToolExecutionPolicy.AUTO) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(db, parent, session_permission(child.id), roots, ToolExecutionPolicy.DISABLED) == PermissionDecision.DENY
    child.parent_session_id = uuid4()
    assert await PermissionPolicy.evaluate(db, parent, session_permission(child.id), roots, ToolExecutionPolicy.AUTO) == PermissionDecision.DENY
    child.parent_session_id = parent.id
    child.user_id = uuid4()
    assert await PermissionPolicy.evaluate(db, parent, session_permission(child.id), roots, ToolExecutionPolicy.AUTO) == PermissionDecision.DENY


@pytest.mark.parametrize("layer", ["default", "integration", "resource", "profile"])
async def test_child_integration_ceiling_cannot_widen_ask_to_auto(tmp_path, monkeypatch, layer):
    integration = IntegrationInstance(name="Calendar", integration_type="calendar", adapter_type="caldav",
                                      config={"base_url": "https://example.org", "username": "private-user"},
                                      credential_id=uuid4())
    integrations = IntegrationSettings(instances=[integration])
    settings = ToolSettings().select_trust(TrustLevel.PUBLIC)
    capability = "calendar.read"
    if layer == "integration":
        settings.permissions.integration_policies[str(integration.id)] = {capability: ToolExecutionPolicy.ASK}
    elif layer == "resource":
        settings.permissions.resource_policies[str(integration.id)] = {"calendar:test": {capability: ToolExecutionPolicy.ASK}}
    elif layer == "profile":
        settings._agent_permissions = AgentProfilePermissions(integration_policies={str(integration.id): {capability: "ask"}})
    settings.public_permission_preset = "ask_all" if layer == "default" else "auto_all"
    snapshot = permission_snapshot(settings, {"calendar_query_events"}, integrations)
    assert "private-user" not in str(snapshot) and str(integration.credential_id) not in str(snapshot)
    parent = _session(user_id=uuid4())
    child = _session(user_id=parent.user_id, parent_session_id=parent.id)
    run = AgentRun(id=uuid4(), session_id=child.id, input_message_id=uuid4(), trust_level="public")
    message = Message(id=run.input_message_id, provider_data={"_matoloop": {"delegationCeiling": snapshot}})
    db = _db_with_grants()
    db.get.side_effect = lambda model, key, **kw: message if model is Message else parent
    _mock_value = AsyncMock(return_value=(ToolSettings(public_permission_preset="auto_all").select_trust(TrustLevel.PUBLIC), None))
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'load_agent_profile_tools', _mock_value)
    _mock_value = AsyncMock(return_value=integrations)
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'load_effective_integrations', _mock_value)
    approve = AsyncMock(return_value="deny")
    monkeypatch.setattr(AgentRuntime, "_request_approval", approve)
    request = PermissionRequest(capability=capability, resource_type="network_service", resource_scope="calendar:test",
                                action_kind="read", risk_level="low", title="Read", summary="Read",
                                policy_integration_id=integration.id, policy_resource_key="calendar:test")
    decision = await AgentRuntime._authorize(db, run, child, ToolCall("read", "calendar_query_events", {}), request, _root_files(tmp_path, child))
    assert decision == PermissionDecision.USER_DENIED
    approve.assert_awaited_once()


@pytest.mark.parametrize(
    ("current", "offered"),
    [
        (current, offered)
        for current in ToolExecutionPolicy
        for offered in ToolExecutionPolicy
    ],
)
def test_external_working_directory_delegation_ceiling(current, offered) -> None:
    settings = ToolSettings(
        public_permissions={"policies": {"working_directory.external": current}}
    ).select_trust(TrustLevel.PUBLIC)
    offered_settings = ToolSettings(
        public_permissions={"policies": {"working_directory.external": offered}}
    ).select_trust(TrustLevel.PUBLIC)
    snapshot = permission_snapshot(settings, {"change_directory"})
    snapshot["offeredCeiling"] = permission_snapshot(
        offered_settings, {"change_directory"}
    )

    assert mock_agent_run_support._external_working_directory_policy(
        settings, snapshot=snapshot
    ) == max(
        (current, offered),
        key=(
            ToolExecutionPolicy.AUTO,
            ToolExecutionPolicy.ASK,
            ToolExecutionPolicy.DISABLED,
        ).index,
    )


@pytest.mark.parametrize("parent_enabled", [None, False, True])
def test_temporary_file_access_cannot_widen_frozen_delegation(parent_enabled):
    settings = ToolSettings()
    settings._temporary_files_enabled = True
    snapshot = permission_snapshot(settings, {"write_file"})
    if parent_enabled is None:
        snapshot.pop("temporaryFilesEnabled")
    else:
        snapshot["temporaryFilesEnabled"] = parent_enabled
    assert apply_delegation_ceiling(settings, snapshot)._temporary_files_enabled is (parent_enabled is True)


def test_missing_delegated_capability_disables_external_working_directory() -> None:
    settings = ToolSettings().select_trust(TrustLevel.PUBLIC)
    snapshot = permission_snapshot(settings, {"change_directory"})
    snapshot["actionDefaults"].pop("working_directory.external")

    assert mock_agent_run_support._external_working_directory_policy(
        settings, snapshot=snapshot
    ) == ToolExecutionPolicy.DISABLED


@pytest.mark.parametrize("offered", [False, True])
async def test_scratch_read_cannot_restore_missing_frozen_capability(tmp_path, monkeypatch, offered):
    session = _session(user_id=uuid4(), preferred_execution_agent_id=uuid4())
    snapshot = permission_snapshot(ToolSettings(), {"read"})
    if offered:
        snapshot["offeredCeiling"] = permission_snapshot(ToolSettings(), {"read"})
    (snapshot["offeredCeiling"] if offered else snapshot)["actionDefaults"].pop("filesystem.read")
    monkeypatch.setattr(mock_agent_runtime, "permission_origin", AsyncMock(return_value=session))
    monkeypatch.setattr(mock_agent_runtime, "delegation_snapshot", AsyncMock(return_value=snapshot))
    assert await AgentRuntime._authorize(
        _db_with_grants(), AgentRun(id=uuid4(), session_id=session.id), session,
        ToolCall("read", "read", {}),
        execution_directory_permission(session.preferred_execution_agent_id, f"/tmp/mato-loop/{session.id}/file", capability="filesystem.read"),
        _root_files(tmp_path, session),
    ) == PermissionDecision.DENY


async def test_other_session_read_requires_approval_without_matching_grant(tmp_path) -> None:
    session = _session()
    db = _db_with_grants()

    decision = await PermissionPolicy.evaluate(
        db,
        session,
        session_permission(uuid4()),
        _root_files(tmp_path, session),
    )

    assert decision == PermissionDecision.ASK
    db.scalars.assert_awaited_once()


@pytest.mark.parametrize(("model_trust", "session_trust", "private_access", "expected"), [
    (TrustLevel.PRIVATE, "private", False, PermissionDecision.ALLOW),
    (TrustLevel.PUBLIC, "public", False, PermissionDecision.ALLOW),
    (TrustLevel.PUBLIC, "private", False, PermissionDecision.DENY),
    (TrustLevel.PUBLIC, "private", True, PermissionDecision.ALLOW),
])
async def test_same_project_session_reads_honor_policy(tmp_path, model_trust, session_trust, private_access, expected) -> None:
    project_id = uuid4()
    session = _session(user_id=uuid4(), project_id=project_id, session_type="chat")
    target = _session(user_id=session.user_id, project_id=project_id, session_type="chat")
    target.trust_level = session_trust
    db = _db_with_grants()
    db.get.return_value = target
    root_files = _root_files(tmp_path, session)
    path_request = project_session_path_permission(
        project_id=project_id,
        session_id=target.id,
        relative_path="files/notes.txt",
        action_kind="read",
        action_label="Datei lesen",
    )

    assert await PermissionPolicy.evaluate(
        db,
        session,
        session_permission(target.id, private_access=private_access),
        root_files,
        ToolExecutionPolicy.AUTO,
        trust_level=model_trust,
    ) == expected
    assert await PermissionPolicy.evaluate(
        db, session, path_request, root_files, ToolExecutionPolicy.AUTO
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        db,
        session,
        path_request,
        root_files,
        ToolExecutionPolicy.DISABLED,
    ) == PermissionDecision.DENY


@pytest.mark.parametrize("trust", list(TrustLevel))
async def test_project_file_grant_reuse_is_exact_and_rechecks_eligibility(tmp_path, trust):
    session = _session(user_id=uuid4(), project_id=uuid4(), session_type="chat")
    target = _session(user_id=session.user_id, project_id=session.project_id, session_type="chat")
    request = project_session_path_permission(
        project_id=session.project_id, session_id=target.id, relative_path="notes.txt",
        action_kind="read", action_label="Datei lesen",
    )
    profile_id = uuid4()
    grant = _grant(
        request, session.id, agent_profile_id=profile_id, trust_level=trust.value
    )
    roots = _root_files(tmp_path, session)
    db = _db_with_grants(grant)
    db.get.return_value = target
    for _ in range(2):
        assert await PermissionPolicy.evaluate(
            db, session, request, roots, ToolExecutionPolicy.ASK,
            agent_profile_id=profile_id, trust_level=trust,
        ) == PermissionDecision.ALLOW
    parameters = db.scalars.await_args.args[0].compile().params.values()
    assert session.id in parameters and profile_id in parameters and trust.value in parameters

    for changed in (
        {"relative_path": "notes.txt/child"}, {"relative_path": "other.txt"},
        {"project_id": uuid4()}, {"session_id": uuid4()},
    ):
        other = project_session_path_permission(**{
            "project_id": session.project_id, "session_id": target.id,
            "relative_path": "notes.txt", "action_kind": "read", "action_label": "Datei lesen",
            **changed,
        })
        assert not PermissionPolicy.grant_matches(grant, other, roots)
    assert await PermissionPolicy.evaluate(
        db, session, request, roots, ToolExecutionPolicy.DISABLED
    ) == PermissionDecision.DENY
    for field, changed in (("user_id", uuid4()), ("project_id", uuid4()),
                           ("parent_session_id", uuid4()), ("session_type", "automation")):
        previous = getattr(target, field)
        setattr(target, field, changed)
        assert await PermissionPolicy.evaluate(
            db, session, request, roots, ToolExecutionPolicy.ASK
        ) == PermissionDecision.DENY
        setattr(target, field, previous)
    db.scalars.return_value.all.return_value = []
    assert await PermissionPolicy.evaluate(
        db, session, request, roots, ToolExecutionPolicy.ASK
    ) == PermissionDecision.ASK


@pytest.mark.parametrize("trust", list(TrustLevel))
@pytest.mark.parametrize("audience", ["private_models", "public_models", "both_models"])
async def test_profile_grants_bind_owner_and_data_audience(tmp_path, trust, audience):
    session = _session(user_id=uuid4())
    roots = _root_files(tmp_path, session)
    request = user_profile_permission(
        session.user_id, field_labels=["Name"], model_scope=audience
    )
    profile_id = uuid4()
    grant = _grant(
        request, session.id, agent_profile_id=profile_id, trust_level=trust.value
    )
    db = _db_with_grants(grant)
    same_audience = user_profile_permission(
        session.user_id, field_labels=["Sprache"], model_scope=audience
    )
    assert await PermissionPolicy.evaluate(
        db, session, same_audience, roots, ToolExecutionPolicy.ASK,
        agent_profile_id=profile_id, trust_level=trust,
    ) == PermissionDecision.ALLOW
    parameters = db.scalars.await_args.args[0].compile().params.values()
    assert session.id in parameters and profile_id in parameters and trust.value in parameters
    for other_audience in {"private_models", "public_models", "both_models"} - {audience}:
        other = user_profile_permission(
            session.user_id, field_labels=["Name"], model_scope=other_audience
        )
        assert await PermissionPolicy.evaluate(
            db, session, other, roots, ToolExecutionPolicy.ASK
        ) == PermissionDecision.ASK
    foreign = user_profile_permission(uuid4(), field_labels=["Name"], model_scope=audience)
    assert await PermissionPolicy.evaluate(
        db, session, foreign, roots, ToolExecutionPolicy.AUTO
    ) == PermissionDecision.DENY
    assert await PermissionPolicy.evaluate(
        db, session, request, roots, ToolExecutionPolicy.DISABLED
    ) == PermissionDecision.DENY
    grant.resource_scope = str(session.user_id)
    assert await PermissionPolicy.evaluate(
        db, session, request, roots, ToolExecutionPolicy.ASK
    ) == PermissionDecision.ASK
    db.scalars.return_value.all.return_value = []
    assert await PermissionPolicy.evaluate(
        db, session, request, roots, ToolExecutionPolicy.ASK
    ) == PermissionDecision.ASK


@pytest.mark.parametrize("scope", [
    "null", "[]", "{}", "not-json",
    '{"modelScope":"both_models","userId":123}',
    '{"modelScope":[],"userId":"00000000-0000-4000-8000-000000000001"}',
    '{"modelScope":"current_model","userId":"00000000-0000-4000-8000-000000000001"}',
])
async def test_invalid_profile_scopes_are_denied(tmp_path, scope):
    session = _session(user_id=uuid4())
    request = user_profile_permission(session.user_id, field_labels=["Name"], model_scope="both_models")
    assert await PermissionPolicy.evaluate(
        _db_with_grants(), session, replace(request, resource_scope=scope),
        _root_files(tmp_path, session), ToolExecutionPolicy.AUTO,
    ) == PermissionDecision.DENY


async def test_session_grant_only_allows_its_exact_target(tmp_path) -> None:
    session = _session()
    granted_target = uuid4()
    grant = _grant(session_permission(granted_target), session.id)
    db = _db_with_grants(grant)
    root_files = _root_files(tmp_path, session)

    assert await PermissionPolicy.evaluate(
        db, session, session_permission(granted_target), root_files
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        db, session, session_permission(uuid4()), root_files
    ) == PermissionDecision.ASK


async def test_media_session_grant_matches_capability_and_current_session(tmp_path) -> None:
    session = _session()
    request = media_permission(session.id, "media.generate.audio", "generate_audio")
    grant = _grant(request, session.id)

    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant), session, request, _root_files(tmp_path, session)
    ) == PermissionDecision.ALLOW
    other_capability = media_permission(
        session.id, "media.generate.speech", "generate_speech"
    )
    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant), session, other_capability, _root_files(tmp_path, session)
    ) == PermissionDecision.ALLOW


async def test_media_model_grant_matches_only_exact_target(tmp_path) -> None:
    session = _session()
    provider_id = uuid4()
    model_id = uuid4()
    request = media_permission(
        session.id,
        "media.generate.image",
        "generate_image",
        provider_id=provider_id,
        model_id=model_id,
        model_name="Image",
        trust_level="public",
    )
    grant = _grant(request, session.id)
    root_files = _root_files(tmp_path, session)

    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant), session, request, root_files
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant),
        session,
        media_permission(
            session.id,
            "media.generate.image",
            "generate_image",
            provider_id=provider_id,
            model_id=uuid4(),
            model_name="Other image",
            trust_level="public",
        ),
        root_files,
    ) == PermissionDecision.ASK


async def test_memory_write_uses_current_session_grant(tmp_path) -> None:
    session = _session()
    request = memory_write_permission(session.id, "create")
    root_files = _root_files(tmp_path, session)

    assert await PermissionPolicy.evaluate(
        _db_with_grants(), session, request, root_files
    ) == PermissionDecision.ASK

    grant = _grant(request, session.id)
    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant),
        session,
        memory_write_permission(session.id, "update"),
        root_files,
    ) == PermissionDecision.ALLOW
    assert request.capability == "memory.write"
    assert request.action_kind == "memory_write"

    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant),
        session,
        memory_write_permission(session.id, "forget"),
        root_files,
    ) == PermissionDecision.ASK


async def test_agent_identity_session_grant_matches_exact_user(tmp_path) -> None:
    session = _session(user_id=uuid4())
    request = agent_identity_permission(
        session.user_id,
        changes_name=True,
        changes_instructions=False,
    )
    grant = _grant(request, session.id)
    root_files = _root_files(tmp_path, session)

    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant), session, request, root_files
    ) == PermissionDecision.ALLOW
    other_user = agent_identity_permission(
        uuid4(),
        changes_name=True,
        changes_instructions=False,
    )
    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant), session, other_user, root_files
    ) == PermissionDecision.ASK


async def test_skill_collection_grant_matches_exact_user_and_capability(tmp_path) -> None:
    session = _session(user_id=uuid4())
    request = skill_load_permission(session.user_id, "Recherche")
    grant = _grant(request, session.id)
    root_files = _root_files(tmp_path, session)

    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant), session, request, root_files
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant),
        session,
        skill_load_permission(uuid4(), "Fremd"),
        root_files,
    ) == PermissionDecision.ASK
    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant),
        session,
        skill_write_permission(session.user_id, "Recherche"),
        root_files,
    ) == PermissionDecision.ASK


async def test_skill_edit_grant_matches_only_exact_skill(tmp_path) -> None:
    session = _session(user_id=uuid4())
    skill_id = uuid4()
    request = skill_edit_permission(skill_id, "Songfinder")
    grant = _grant(request, session.id)
    collection_grant = _grant(
        skill_write_permission(session.user_id, "Recherche"), session.id
    )
    root_files = _root_files(tmp_path, session)

    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant), session, request, root_files
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant),
        session,
        skill_edit_permission(uuid4(), "Anderer Skill"),
        root_files,
    ) == PermissionDecision.ASK
    assert await PermissionPolicy.evaluate(
        _db_with_grants(collection_grant), session, request, root_files
    ) == PermissionDecision.ASK


async def test_session_search_and_session_read_grants_remain_separate(tmp_path) -> None:
    session = _session()
    target_id = uuid4()
    read_grant = _grant(session_permission(target_id), session.id)
    search_grant = _grant(session_search_permission(), session.id)
    root_files = _root_files(tmp_path, session)

    assert await PermissionPolicy.evaluate(
        _db_with_grants(read_grant), session, session_search_permission(), root_files
    ) == PermissionDecision.ASK
    assert await PermissionPolicy.evaluate(
        _db_with_grants(search_grant), session, session_permission(target_id), root_files
    ) == PermissionDecision.ASK
    assert await PermissionPolicy.evaluate(
        _db_with_grants(search_grant), session, session_search_permission(), root_files
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        _db_with_grants(search_grant), session, session_browse_permission(), root_files
    ) == PermissionDecision.ALLOW


def test_webfetch_permission_hashes_and_displays_the_full_canonical_url() -> None:
    first = webfetch_permission("HTTPS://Example.com/path?token=secret#fragment")
    equivalent = webfetch_permission("https://example.com/path?token=secret")
    other_query = webfetch_permission("https://example.com/path?token=other")

    assert first.capability == "network.fetch"
    assert first.resource_type == "url"
    assert first.resource_scope == equivalent.resource_scope
    assert first.resource_scope != other_query.resource_scope
    assert len(first.resource_scope) == 64
    assert "https://example.com/path?token=secret" in first.summary
    assert first.resource_display == "https://example.com/path?token=secret"


def test_transcript_permission_normalizes_equivalent_video_urls() -> None:
    watch = transcript_permission(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=12"
    )
    short = transcript_permission("https://youtu.be/dQw4w9WgXcQ")
    other = transcript_permission("https://youtu.be/abcdefghijk")

    assert watch.resource_scope == short.resource_scope
    assert watch.resource_scope != other.resource_scope
    assert watch.resource_display == "https://youtu.be/dQw4w9WgXcQ"
    assert watch.action_kind == "fetch_transcript"


async def test_webfetch_grant_matches_only_the_exact_url_hash(tmp_path) -> None:
    session = _session()
    granted = webfetch_permission("https://example.com/page?lang=de")
    grant = _grant(granted, session.id)
    root_files = _root_files(tmp_path, session)

    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant), session, granted, root_files
    ) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        _db_with_grants(grant),
        session,
        webfetch_permission("https://example.com/page?lang=en"),
        root_files,
    ) == PermissionDecision.ASK


async def test_sysadmin_arcane_save_rechecks_real_grants_after_secret_resolution(tmp_path, monkeypatch):
    profile = sysadmin_profile()
    instance = IntegrationInstance(name="Host", integration_type="infrastructure", adapter_type="arcane",
                                   config={"base_url": "https://arcane.example.test"},
                                   profile_permissions={profile.id: IntegrationPermissions(available=True)})
    integrations = IntegrationSettings(instances=[instance])
    settings = ToolSettings().select_trust(TrustLevel.PUBLIC)
    session = _session(user_id=uuid4(), agent_profile_id=profile.id)
    run = AgentRun(id=uuid4(), session_id=session.id, agent_profile_id=profile.id, trust_level="public")
    secret_id = uuid4()
    arguments = arcane_module.ArcaneProjectSave(integration_id=instance.id, environment_id="0", name="app",
        compose_content="services: {}", env_content={"parts": ["VALUE=", {"value_ref": f"run-secret:{secret_id}"}]})
    call = ToolCall("save", "arcane_project_save", arguments.model_dump(mode="json"))
    context = SimpleNamespace(session=session, integration_settings=integrations, one_time_secrets={secret_id: "private"},
        current_integrations=AsyncMock(return_value=integrations), bound_options={"integration_id": str(instance.id),
            "binding_revision": json.dumps(instance.binding_data(), ensure_ascii=True, separators=(",", ":"), sort_keys=True)})
    request = arcane_module._project_save_permission(arguments, context)[0]
    grant = _grant(request, session.id, run_id=run.id, agent_profile_id=profile.id, trust_level="public")
    db = _db_with_grants(grant)
    db.scalar.return_value = None
    roots = _root_files(tmp_path, session)
    monkeypatch.setattr(mock_agent_runtime, "permission_origin", AsyncMock(return_value=session))
    monkeypatch.setattr(mock_agent_runtime, "delegation_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(mock_agent_runtime, "load_agent_profile_tools", AsyncMock(return_value=(settings, profile)))
    monkeypatch.setattr(mock_agent_runtime, "load_effective_integrations", AsyncMock(return_value=integrations))

    async def authorize(request, *, check_only=False):
        assert check_only
        return await AgentRuntime._authorize(db, run, session, call, request, roots, check_only=True)

    context.ask_handler = authorize
    remote = AsyncMock(return_value=({"id": "created"}, None))

    @asynccontextmanager
    async def client(_arguments, _context):
        yield SimpleNamespace(request=remote)

    monkeypatch.setattr(arcane_module, "_client", client)
    assert not (await arcane_module.arcane_project_save(arguments, context)).is_error
    resolve = arcane_module.resolve_protected_text

    async def revoke(context, text):
        value = await resolve(context, text)
        if not isinstance(text, str):
            db.scalars.return_value.all.return_value = []
        return value

    monkeypatch.setattr(arcane_module, "resolve_protected_text", revoke)
    result = await arcane_module.arcane_project_save(arguments, context)
    assert result.is_error and "permission.denied" in result.content and remote.await_count == 1


@pytest.mark.parametrize("trust", list(TrustLevel))
async def test_sysadmin_arcane_config_uses_sensitive_read_guard(tmp_path, monkeypatch, trust):
    profile = sysadmin_profile()
    instance = IntegrationInstance(name="Host", integration_type="infrastructure", adapter_type="arcane",
        config={"base_url": "https://arcane.example.test"}, profile_permissions={profile.id: IntegrationPermissions(available=True)})
    integrations = IntegrationSettings(instances=[instance])
    settings = ToolSettings(private_permission_preset="balanced", public_permission_preset="balanced").select_trust(trust)
    settings.permissions.integration_policies[str(instance.id)] = {"arcane.read": ToolExecutionPolicy.AUTO}
    settings._agent_permissions = getattr(profile, f"{trust.value}_permissions")
    session = _session(user_id=uuid4(), agent_profile_id=profile.id)
    run = AgentRun(id=uuid4(), session_id=session.id, agent_profile_id=profile.id, trust_level=trust.value)
    db = _db_with_grants()
    db.scalar.return_value = None
    roots = _root_files(tmp_path, session)
    monkeypatch.setattr(mock_agent_runtime, "permission_origin", AsyncMock(return_value=session))
    monkeypatch.setattr(mock_agent_runtime, "delegation_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(mock_agent_runtime, "load_agent_profile_tools", AsyncMock(return_value=(settings, profile)))
    monkeypatch.setattr(mock_agent_runtime, "load_effective_integrations", AsyncMock(return_value=integrations))
    context = SimpleNamespace(integration_settings=integrations, bound_options={"integration_id": str(instance.id),
        "binding_revision": json.dumps(instance.binding_data(), ensure_ascii=True, separators=(",", ":"), sort_keys=True)})
    summary = arcane_module.ArcaneGet(integration_id=instance.id, environment_id="0", kind="projects", resource_id="app")
    config = arcane_module.ArcaneProjectConfig(integration_id=instance.id, environment_id="0", project_id="app")
    decisions = []
    for name, arguments, request in (
        ("arcane_get", summary, arcane_module._read_permission(summary, context)[0]),
        ("arcane_project_config", config, arcane_module._project_config_permission(config, context)[0]),
    ):
        decisions.append(await AgentRuntime._authorize(db, run, session,
            ToolCall(name, name, arguments.model_dump(mode="json")), request, roots, check_only=True))
    assert decisions == [PermissionDecision.ALLOW, PermissionDecision.ASK]
