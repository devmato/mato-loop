import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from io import BytesIO
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import mato_loop_backend.tools.interaction as mock_tools_interaction
from mato_loop_backend.agent.question_decisions import answer_question
from mato_loop_backend.agent.repository_instructions import repository_instructions_prompt
from mato_loop_backend.agent.run_contracts import ApprovalResolution
from mato_loop_backend.agent.run_support import _external_working_directory_policy, _tool_policy
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.agent.session_secrets import session_secrets
from mato_loop_backend.agent.subagents import permission_snapshot
from mato_loop_backend.execution import (
    DirectoryResult,
    ExecutionClient,
    ExecutionClientError,
    ProcessResult,
    ShellInspectResult,
    WorkspacePatchResult,
    WorkspaceReadResult,
    WorkspaceWriteResult,
    transfer_tickets,
)
from mato_loop_backend.execution import (
    transfers as transfers_module,
)
from mato_loop_backend.execution.client import (
    WorkspaceExportResult,
    WorkspaceListResult,
    WorkspaceStatResult,
)
from mato_loop_backend.execution.protocol import ResponseMessage, SystemInfo
from mato_loop_backend.execution.terminals import (
    BrowserTerminalConnection,
    TerminalRuntime,
    terminal_coordinator,
)
from mato_loop_backend.execution.transfers import (
    EXPORT_TIMEOUT_SECONDS,
    MAX_TRANSFER_TTL_SECONDS,
    TRANSFER_TTL_SECONDS,
    TransferError,
    TransferTicketStore,
)
from mato_loop_backend.files.access import execution_path
from mato_loop_backend.files.service import RootFileService, SessionStorage
from mato_loop_backend.models import (
    AgentRun,
    ChatSession,
    Credential,
    ExecutionAgent,
    PermissionGrant,
    QuestionRequest,
)
from mato_loop_backend.permissions.policy import (
    PermissionDecision,
    PermissionDisplayItem,
    PermissionPolicy,
    PermissionRequest,
    SessionGrantScope,
    administrator_permission,
    execution_directory_permission,
    execution_directory_scope,
    session_grant_matches_request,
    shell_action_scope,
    shell_command_permissions,
    shell_command_scope,
    shell_prefix_allowed,
)
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings import (
    IntegrationSettings,
    ToolExecutionPolicy,
    ToolSettings,
    TrustLevel,
    build_profile,
    plan_profile,
    sysadmin_profile,
)
from mato_loop_backend.tools.administrator import AdministratorAuthenticationResult
from mato_loop_backend.tools.arguments import ProtectedQuestionResolution, ShellRunArguments
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.tools.computer import _authorize_shell, _run_process, _shell_run
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.process_output import redact_process_output


class FakeExecutionClient:
    def __init__(self, responses, operations, current_directory="/home/user/project") -> None:
        self.agent_id = uuid4()
        self.current_directory = current_directory
        self.operations = frozenset(operations)
        self.responses = responses
        self.requests = []

    def supports(self, operation: str) -> bool:
        return operation in self.operations

    async def request(self, operation, arguments, _result_model, **_kwargs):
        self.requests.append((operation, arguments))
        if operation == "administrator.run" and arguments["operation"] == "authenticate":
            return self.responses.get("authenticate", AdministratorAuthenticationResult(passwordless=True))
        if operation == "shell.classify" or operation == "administrator.run" and arguments["operation"] == "shell.inspect":
            return ShellInspectResult(
                required_categories=["read"],
                classification="read", read_only=True, analysis_complete=operation != "shell.classify",
                inspection_revision="a" * 64, targets=[], commands=[],
            )
        response = self.responses[operation]
        return response(arguments) if callable(response) else response


def _context(tmp_path, call, client, *, decision=PermissionDecision.ALLOW):
    session = ChatSession(
        id=uuid4(),
        user_id=uuid4(),
        title="Test",
        memory_mode="disabled",
        web_search_policy="available",
        current_directory=client.current_directory,
    )
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    return ToolContext(
        db=AsyncMock(),
        run_id=uuid4(),
        session=session,
        tool_call=call,
        root_files=RootFileService(session.id, session_storage=storage),
        integration_settings=IntegrationSettings(),
        ask_handler=AsyncMock(return_value=decision),
        tool_settings=ToolSettings(),
        execution_client=client,
    )


def _tools(operations):
    return tool_registry.effective(
        IntegrationSettings(),
        tool_settings=ToolSettings(),
        execution_operations=frozenset(operations),
    )


def test_shell_tool_cannot_select_execution_targets() -> None:
    tools = _tools({"shell.run"})
    shell = next(item for item in tools.schemas() if item["name"] == "shell_run")
    assert "secret_input" in tools.tools
    secret_input = next(
        item for item in tools.schemas() if item["name"] == "secret_input"
    )
    assert set(secret_input["parameters"]["properties"]) == {"inputs"}
    assert set(shell["parameters"]["properties"]) == {
        "_reason",
        "command",
        "environment",
        "timeout",
        "elevated",
        "sudo_password_ref",
    }
    assert shell["parameters"]["required"] == ["command"]
    assert "secret_input with administrator=true" in shell["description"]
    assert "command's purpose" in shell["parameters"]["properties"]["_reason"]["description"]
    assert ShellRunArguments(command="pwd").timeout is None
    assert not ShellRunArguments(command="pwd").elevated
    with pytest.raises(ValidationError):
        ShellRunArguments(command="pwd", sudo_password_ref=uuid4())
    assert all(item["name"] != "process_exec" for item in tools.schemas())
    assert {
        action.capability: (
            action.label,
            action.default_policy(ToolSettings().select_trust(TrustLevel.PRIVATE)),
            action.default_policy(ToolSettings().select_trust(TrustLevel.PUBLIC)),
        )
        for action in tool_registry._operation_definitions["shell_run"].actions
    } == {
        "shell.read": (
            "Shell ohne erkannte Schreibaktionen",
            ToolExecutionPolicy.AUTO,
            ToolExecutionPolicy.ASK,
        ),
        "shell.write": (
            "Als schreibend eingestufte Shell",
            ToolExecutionPolicy.ASK,
            ToolExecutionPolicy.ASK,
        ),
        "shell.delete": (
            "Erkannte Shell-Löschaktionen",
            ToolExecutionPolicy.ASK,
            ToolExecutionPolicy.ASK,
        ),
    }


async def test_execution_client_forwards_exact_arguments_without_context(monkeypatch) -> None:
    agent_id = uuid4()
    client = ExecutionClient(agent_id, frozenset({"filesystem.read"}), "/home/user")
    request = AsyncMock(
        return_value=ResponseMessage.model_validate(
            {
                "type": "response",
                "id": uuid4(),
                "ok": True,
                "result": {
                    "content": "hello",
                    "linesRead": 1,
                    "hasMore": False,
                    "nextOffset": None,
                    "truncatedLines": [],
                    "sha256": "b" * 64,
                },
            }
        )
    )
    monkeypatch.setattr(
        "mato_loop_backend.execution.client.execution_agent_connections.request", request
    )

    await client.request(
        "filesystem.read", {"path": "/etc/hosts"}, WorkspaceReadResult
    )

    request.assert_awaited_once_with(
        agent_id, "filesystem.read", {"path": "/etc/hosts"}, timeout=None, on_event=None
    )


async def test_execution_client_rejects_relative_result_path(monkeypatch) -> None:
    client = ExecutionClient(uuid4(), frozenset({"filesystem.write"}))
    monkeypatch.setattr(
        "mato_loop_backend.execution.client.execution_agent_connections.request",
        AsyncMock(
            return_value=ResponseMessage.model_validate(
                {
                    "type": "response",
                    "id": uuid4(),
                    "ok": True,
                    "result": {"path": "relative.txt", "sha256": "a" * 64, "size": 1},
                }
            )
        ),
    )
    with pytest.raises(ExecutionClientError) as error:
        await client.request(
            "filesystem.write", {"path": "/tmp/file"}, WorkspaceWriteResult
        )
    assert error.value.code == "executionAgent.protocolError"


def test_execution_result_accepts_the_filesystem_root() -> None:
    assert DirectoryResult(path="/").path == "/"


async def test_execution_client_imports_absolute_path_with_transfer_ticket(monkeypatch) -> None:
    agent_id = uuid4()
    data = b"managed snapshot"
    digest = hashlib.sha256(data).hexdigest()
    client = ExecutionClient(agent_id, frozenset({"filesystem.import"}))
    monkeypatch.setattr(
        "mato_loop_backend.execution.client.execution_agent_connections.transfer_url",
        lambda _agent_id, ticket_id: f"https://backend.test/transfers/{ticket_id}",
    )

    async def request(selected_agent_id, _operation, arguments, **kwargs):
        assert kwargs["timeout"] == EXPORT_TIMEOUT_SECONDS
        ticket_id = next(
            ticket_id
            for ticket_id in transfer_tickets._tickets
            if str(ticket_id) in arguments["transfer"]["url"]
        )
        ticket = transfer_tickets.claim(
            ticket_id,
            arguments["transfer"]["token"],
            "download",
            owner_id=selected_agent_id,
        )
        assert ticket.content is not None and ticket.content.read() == data
        return ResponseMessage.model_validate(
            {
                "type": "response",
                "id": uuid4(),
                "ok": True,
                "result": {
                    "path": "/tmp/snapshot.bin",
                    "size": len(data),
                    "sha256": digest,
                },
            }
        )

    monkeypatch.setattr(
        "mato_loop_backend.execution.client.execution_agent_connections.request", request
    )
    result = await client.write_bytes(
        "/tmp/snapshot.bin", BytesIO(data), limit=len(data)
    )
    assert result.path == "/tmp/snapshot.bin"


async def test_cancelled_import_drains_staging_before_releasing_source(monkeypatch):
    started = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    staged = BytesIO(b"staged")

    def stage(source, **_kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5) and not source.closed
        return staged, 6, hashlib.sha256(b"staged").hexdigest()

    monkeypatch.setattr("mato_loop_backend.execution.client.stage_file", stage)
    with BytesIO(b"source") as source:
        pending = asyncio.create_task(ExecutionClient(uuid4(), frozenset()).write_bytes("/tmp/file", source, limit=10))
        try:
            await asyncio.wait_for(started.wait(), 5)
            pending.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert staged.closed


def test_upload_ticket_ttl_override_is_bounded_and_keeps_default(monkeypatch) -> None:
    monkeypatch.setattr(transfers_module.time, "monotonic", lambda: 100.0)
    store = TransferTicketStore()

    _, default_ticket = store.create_upload(uuid4(), size=1)
    _, export_ticket = store.create_upload(
        uuid4(), size=1, ttl_seconds=EXPORT_TIMEOUT_SECONDS + 30
    )

    assert default_ticket.expires_at == 100.0 + TRANSFER_TTL_SECONDS
    assert export_ticket.expires_at == 100.0 + EXPORT_TIMEOUT_SECONDS + 30
    with pytest.raises(TransferError):
        store.create_upload(uuid4(), size=1, ttl_seconds=MAX_TRANSFER_TTL_SECONDS + 1)


async def test_execution_export_uses_long_lived_upload_ticket(monkeypatch) -> None:
    data = b"export"
    digest = hashlib.sha256(data).hexdigest()
    captured = []
    create_upload = transfer_tickets.create_upload

    def capture_upload(*args, **kwargs):
        grant, ticket = create_upload(*args, **kwargs)
        ticket.content = BytesIO(data)
        ticket.actual_size = len(data)
        ticket.actual_sha256 = digest
        captured.append(ticket)
        return grant, ticket

    async def request(_self, operation, _arguments, _result_model, **kwargs):
        if operation == "filesystem.stat":
            return WorkspaceStatResult(
                path="/tmp/export.bin", kind="file", size=len(data), modified_at=0.0
            )
        assert operation == "filesystem.export"
        assert kwargs["timeout"] == EXPORT_TIMEOUT_SECONDS
        return WorkspaceExportResult(size=len(data), sha256=digest)

    monkeypatch.setattr(transfer_tickets, "create_upload", capture_upload)
    monkeypatch.setattr(ExecutionClient, "request", request)
    monkeypatch.setattr(
        "mato_loop_backend.execution.client.execution_agent_connections.transfer_url",
        lambda _agent_id, ticket_id: f"https://backend.test/transfers/{ticket_id}",
    )
    before = transfers_module.time.monotonic()

    with await ExecutionClient(uuid4(), frozenset()).open_export(
        "/tmp/export.bin", limit=len(data)
    ) as content:
        assert content.read() == data

    assert captured[0].expires_at >= before + EXPORT_TIMEOUT_SECONDS + 29


def test_model_paths_resolve_against_current_directory(tmp_path) -> None:
    context = _context(
        tmp_path,
        ToolCall("call", "read", {}),
        FakeExecutionClient({}, {"filesystem.read"}),
    )
    assert execution_path(context, "src/../README.md") == "/home/user/project/README.md"
    assert execution_path(context, "/etc/hosts") == "/etc/hosts"


async def test_change_to_current_directory_does_not_request_approval(tmp_path) -> None:
    path = "/home/user/project"
    call = ToolCall("call", "change_directory", {"path": path})
    client = FakeExecutionClient(
        {"filesystem.resolve": DirectoryResult(path=path)}, {"filesystem.resolve"}, path,
    )
    context = _context(tmp_path, call, client, decision=PermissionDecision.USER_DENIED)

    result = await _tools(client.operations).execute(call, context)

    assert json.loads(result.content) == {"currentDirectory": path}


async def test_workspace_read_dispatches_absolute_filesystem_path(tmp_path) -> None:
    call = ToolCall(
        "call",
        "read",
        {"root": "workspace", "path": "README.md", "offset": 1, "limit": 20},
    )
    client = FakeExecutionClient(
        {
            "filesystem.stat": WorkspaceStatResult(
                path="/home/user/project/README.md",
                kind="file",
                size=5,
                modified_at=0.0,
            ),
            "filesystem.read": WorkspaceReadResult(
                content="hello",
                lines_read=1,
                has_more=False,
                next_offset=None,
                truncated_lines=[],
                sha256="a" * 64,
            ),
        },
        {"filesystem.stat", "filesystem.read"},
    )
    context = _context(tmp_path, call, client)
    result = await _tools(client.operations).execute(call, context)
    assert not result.is_error
    assert client.requests[0] == (
        "filesystem.stat",
        {"path": "/home/user/project/README.md"},
    )


@pytest.mark.parametrize("decision", [PermissionDecision.ASK, PermissionDecision.DENY])
async def test_repository_prefetch_does_not_access_unapproved_paths(decision) -> None:
    client = FakeExecutionClient({}, {"filesystem.stat", "filesystem.read"}, "/work")
    checked = {}
    prompt = await repository_instructions_prompt(client, [], AsyncMock(return_value=decision), checked_entries=checked)
    assert client.requests == []
    assert "Some repository instructions could not be loaded." in prompt
    assert list(checked.values()) == [{
        "path": "/work/AGENTS.md", "directory": "/work",
        "status": "approval_required" if decision == PermissionDecision.ASK else "denied",
    }]


@pytest.mark.parametrize("kind,size,status", [
    ("symlink", 12, "file.unsupportedType"),
    ("file", 33 * 1024, "file.tooLarge"),
    ("file", 12, "missing"),
])
async def test_repository_prefetch_reports_unreadable_files(kind, size, status) -> None:
    def stat(arguments):
        path = arguments["path"]
        if path.endswith("AGENTS.md") and status == "missing":
            raise ExecutionClientError("file.pathNotFound")
        return WorkspaceStatResult(path=path, kind="directory" if path.endswith(".git") else kind,
                                   size=size, modified_at=0.0)

    client = FakeExecutionClient({"filesystem.stat": stat}, {"filesystem.stat", "filesystem.read"}, "/work")
    checked = {}
    prompt = await repository_instructions_prompt(client, [], AsyncMock(return_value=PermissionDecision.ALLOW), checked_entries=checked)
    assert ("Some repository instructions could not be loaded." in prompt) == (status != "missing")
    assert checked["/work/AGENTS.md"]["status"] == status
    assert all(operation != "filesystem.read" for operation, _arguments in client.requests)


@pytest.mark.parametrize("root", ["/work", "C:/work"])
@pytest.mark.parametrize("external", [PermissionDecision.ALLOW, PermissionDecision.ASK])
async def test_repository_prefetch_stops_at_git_and_permission_boundaries(root, external) -> None:
    cwd = root + "/src"

    def stat(arguments):
        path = arguments["path"]
        if path == root + "/.git" or path.endswith("AGENTS.md"):
            return WorkspaceStatResult(path=path, kind="file", size=10, modified_at=0.0)
        raise ExecutionClientError("file.pathNotFound")

    async def authorize(request):
        return PermissionDecision.ALLOW if json.loads(request.resource_scope)["path"].startswith(cwd + "/") else external

    client = FakeExecutionClient({
        "filesystem.stat": stat,
        "filesystem.read": WorkspaceReadResult(content="Use conventions.", lines_read=1, has_more=False,
                                               truncated_lines=[], sha256="a" * 64),
    }, {"filesystem.stat", "filesystem.read"}, cwd)
    prompt = await repository_instructions_prompt(client, [], authorize)
    assert (
        "Do not rediscover AGENTS.md files yourself. "
        "Read explicitly referenced guidelines when relevant to the current task." in prompt
    )
    assert [entry["path"] for entry in json.loads(prompt.splitlines()[-2])["files"]] == (
        [root + "/AGENTS.md", cwd + "/AGENTS.md"] if external == PermissionDecision.ALLOW
        else [cwd + "/AGENTS.md"]
    )


@pytest.mark.parametrize("child_status", ["missing", "unreadable", "approval_changed"])
async def test_repository_prefetch_keeps_rules_stable_when_discovery_changes(child_status) -> None:
    git_allowed = False

    def stat(arguments):
        path = arguments["path"]
        if path == "/work/other/AGENTS.md" or (path == "/work/src/AGENTS.md" and child_status == "missing"):
            raise ExecutionClientError("file.pathNotFound")
        return WorkspaceStatResult(path=path, kind="directory" if path.endswith(".git") else "file",
                                   size=33 * 1024 if path == "/work/src/AGENTS.md" else 16, modified_at=0.0)

    async def authorize(request):
        path = json.loads(request.resource_scope)["path"]
        if path == "/work/src/AGENTS.md" and child_status == "approval_changed":
            return PermissionDecision.DENY if git_allowed else PermissionDecision.ASK
        return PermissionDecision.ASK if not git_allowed and path.endswith(".git") else PermissionDecision.ALLOW

    client = FakeExecutionClient({
        "filesystem.stat": stat,
        "filesystem.read": WorkspaceReadResult(content="Use conventions.", lines_read=1, has_more=False,
                                               truncated_lines=[], sha256="a" * 64),
    }, {"filesystem.stat", "filesystem.read"}, "/work")
    before = await repository_instructions_prompt(client, [], authorize, target_paths=("/work/src/main.py",))
    git_allowed = True
    after = await repository_instructions_prompt(client, [], authorize,
        target_paths=("/work/src/main.py", "/work/other/main.py"))
    assert after == before


async def test_repository_prefetch_reports_partial_context_budget_omission() -> None:
    def stat(arguments):
        path = arguments["path"]
        return WorkspaceStatResult(path=path, kind="directory" if path.endswith(".git") else "file",
                                   size=2_000, modified_at=0.0)

    def read(arguments):
        return WorkspaceReadResult(
            content="x" * 2_000 if arguments["path"] == "/work/src/AGENTS.md" else "Root rules.",
            lines_read=1, has_more=False, truncated_lines=[], sha256="a" * 64,
        )

    fits = AsyncMock(side_effect=lambda context: len(context) < 1_500)

    client = FakeExecutionClient({"filesystem.stat": stat, "filesystem.read": read},
                                 {"filesystem.stat", "filesystem.read"}, "/work")
    prompt = await repository_instructions_prompt(
        client, [], AsyncMock(return_value=PermissionDecision.ALLOW),
        target_paths=("/work/src/main.py",), fits_context=fits,
    )
    assert "Some repository instructions could not be loaded." in prompt
    assert fits.await_count == 2


async def test_workspace_read_sends_generated_image_to_model(tmp_path) -> None:
    image = BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, format="PNG")
    call = ToolCall("image", "read", {"root": "workspace", "path": "scanrand_probe.png"})
    client = FakeExecutionClient({
        "filesystem.stat": WorkspaceStatResult(
            path="/home/user/project/scanrand_probe.png", kind="file",
            size=len(image.getvalue()), modified_at=0.0,
        ),
    }, {"filesystem.stat", "filesystem.export"})
    client.read_bytes = AsyncMock(return_value=image.getvalue())
    result = await _tools(client.operations).execute(call, _context(tmp_path, call, client))

    assert not result.is_error
    assert json.loads(result.content)["image"]["mimeType"] == "image/png"
    assert result.attachments[0].ephemeral is True
    assert result.attachments[0].data == image.getvalue()
    client.read_bytes.assert_awaited_once_with(
        "/home/user/project/scanrand_probe.png", limit=64 * 1024 * 1024,
    )


async def test_workspace_directory_read_grants_only_that_directory(tmp_path) -> None:
    call = ToolCall(
        "call",
        "read",
        {"root": "workspace", "path": "/etc", "offset": 1, "limit": 20},
    )
    client = FakeExecutionClient(
        {
            "filesystem.stat": WorkspaceStatResult(
                path="/etc", kind="directory", size=None, modified_at=0.0
            ),
            "filesystem.list": WorkspaceListResult(
                entries=[], has_more=False, next_offset=None
            ),
        },
        {"filesystem.stat", "filesystem.list"},
    )
    context = _context(tmp_path, call, client)

    result = await _tools(client.operations).execute(call, context)

    assert not result.is_error
    request = context.ask_handler.await_args.args[0]
    assert request.effective_session_grants == (
        SessionGrantScope(
            "read",
            "execution_path",
            execution_directory_scope(client.agent_id, "/etc", recursive=True),
            "/etc",
        ),
    )


async def test_workspace_write_uses_compare_and_swap_operation(tmp_path) -> None:
    call = ToolCall(
        "write",
        "write_file",
        {
            "root": "workspace",
            "path": "README.md",
            "content": "updated\n",
            "expectedSha256": "a" * 64,
        },
    )
    client = FakeExecutionClient(
        {
            "filesystem.write": WorkspaceWriteResult(
                path="/home/user/project/README.md",
                sha256="b" * 64,
                size=8,
            )
        },
        {"filesystem.write"},
    )
    context = _context(tmp_path, call, client)

    result = await _tools(client.operations).execute(call, context)

    assert not result.is_error
    assert client.requests == [
        (
            "filesystem.write",
            {
                "path": "/home/user/project/README.md",
                "content": "updated\n",
                "expectedSha256": "a" * 64,
            },
        )
    ]


@pytest.mark.parametrize(("tool", "operation", "arguments", "response"), [
    ("read", "filesystem.read", {}, WorkspaceReadResult(content="old", lines_read=1, has_more=False, next_offset=None, truncated_lines=[], sha256="a" * 64)),
    ("write_file", "filesystem.write", {"content": "new", "expectedSha256": "a" * 64}, WorkspaceWriteResult(path="/etc/demo.conf", sha256="b" * 64, size=3)),
    ("edit_file", "filesystem.edit", {"oldText": "old", "newText": "new", "expectedSha256": "a" * 64}, WorkspaceWriteResult(path="/etc/demo.conf", sha256="b" * 64, size=3)),
])
async def test_elevated_files_use_structured_worker_and_existing_permissions(tmp_path, tool, operation, arguments, response):
    call = ToolCall("file", tool, {"root": "computer", "path": "/etc/demo.conf", "elevated": True, **arguments})
    client = FakeExecutionClient({"administrator.run": response}, {"administrator.run"})
    context = _context(tmp_path, call, client)
    result = await _tools(client.operations).execute(call, context)
    assert not result.is_error
    assert client.requests[-1][1]["operation"] == operation
    assert context.ask_handler.await_args.args[0].capability == ("filesystem.read" if tool == "read" else "filesystem.write")


@pytest.mark.parametrize("mode", ["update", "move", "move_denied"])
async def test_apply_patch_resolves_paths_and_checks_each_permission(tmp_path, mode) -> None:
    patch = """*** Begin Patch
*** Update File: README.md
@@
-old
+new
*** Add File: src/new.py
+value = 1
*** End Patch
"""
    moving = mode != "update"
    if moving:
        patch = patch.replace("*** Update File: README.md\n@@", "*** Update File: README.md\n*** Move to: renamed.md")
    call = ToolCall(
        "patch",
        "apply_patch",
        {"patch": patch, "expectedSha256": {"README.md": "a" * 64}},
    )
    client = FakeExecutionClient(
        {
            "filesystem.patch": WorkspacePatchResult(
                files=[
                    {
                        "operation": "move" if moving else "update",
                        "path": "/home/user/project/README.md",
                        **({"destination": "/home/user/project/renamed.md"} if moving else {}),
                        "sha256": "b" * 64,
                        "size": 4,
                    },
                    {
                        "operation": "add",
                        "path": "/home/user/project/src/new.py",
                        "sha256": "c" * 64,
                        "size": 10,
                    },
                ]
            )
        },
        {"filesystem.patch"},
    )
    context = _context(tmp_path, call, client)
    if mode == "move_denied":
        async def authorize(request):
            if (request.capability == "filesystem.write"
                    and json.loads(request.resource_scope)["path"] == "/home/user/project/README.md"):
                return PermissionDecision.DENY
            return PermissionDecision.ALLOW
        context.ask_handler.side_effect = authorize

    result = await _tools(client.operations).execute(call, context)

    if mode == "move_denied":
        assert result.is_error and not client.requests
        return
    assert not result.is_error
    assert client.requests == [
        (
            "filesystem.patch",
            {
                "patch": patch.replace(
                    "README.md", "/home/user/project/README.md"
                ).replace("src/new.py", "/home/user/project/src/new.py")
                .replace("renamed.md", "/home/user/project/renamed.md"),
                "expectedSha256": {
                    "/home/user/project/README.md": "a" * 64
                },
            },
        )
    ]
    assert [
        request.args[0].capability for request in context.ask_handler.await_args_list
    ] == (["filesystem.delete", "filesystem.write", "filesystem.write", "filesystem.write"]
          if moving else ["filesystem.write", "filesystem.write"])


async def test_incomplete_patch_error_propagates_remote_params_through_registry(
    tmp_path, monkeypatch,
) -> None:
    params = {
        "completedOperations": 1,
        "totalOperations": 2,
        "failedOperationIndex": 1,
        "path": "/home/user/project/source",
        "destination": "/home/user/project/destination",
    }
    client = ExecutionClient(uuid4(), frozenset({"filesystem.patch"}), "/home/user/project")
    request = AsyncMock(return_value=ResponseMessage.model_validate({
        "type": "response",
        "id": uuid4(),
        "ok": False,
        "error": {"code": "file.patchIncomplete", "params": params},
    }))
    monkeypatch.setattr(
        "mato_loop_backend.execution.client.execution_agent_connections.request", request
    )
    call = ToolCall("patch", "apply_patch", {
        "patch": "*** Begin Patch\n*** Add File: first\n+first\n"
        "*** Update File: source\n*** Move to: destination\n*** End Patch\n",
        "expectedSha256": {"source": "a" * 64},
    })

    result = await _tools(client.operations).execute(call, _context(tmp_path, call, client))

    assert result.is_error
    descriptor = json.loads(result.content)["error"]
    assert descriptor["code"] == "file.patchIncomplete"
    assert descriptor["params"] == params
    assert "Reread the affected paths before retrying" in descriptor["message"]
    request.assert_awaited_once()


async def test_execution_error_params_are_validated_before_tool_serialization(tmp_path) -> None:
    def fail(_arguments):
        raise ExecutionClientError("file.patchIncomplete", {"invalid": object()})

    client = FakeExecutionClient({"filesystem.patch": fail}, {"filesystem.patch"})
    call = ToolCall("patch", "apply_patch", {
        "patch": "*** Begin Patch\n*** Add File: first\n+first\n*** End Patch\n",
        "expectedSha256": {},
    })

    result = await _tools(client.operations).execute(call, _context(tmp_path, call, client))

    assert result.is_error
    descriptor = json.loads(result.content)["error"]
    assert descriptor["code"] == "file.patchIncomplete"
    assert "params" not in descriptor


def _shell_permission(
    client, classification="write", prefixes=None, directory=None, paths=None, analysis_complete=True
):
    action = shell_action_scope(client.agent_id, directory or client.current_directory, json.dumps(prefixes or []))
    return PermissionRequest(
        capability=f"shell.{classification}",
        resource_type="shell_command",
        resource_scope=shell_command_scope(
            client.agent_id,
            directory or client.current_directory,
            classification,
            prefixes or [],
            paths or [],
            analysis_complete,
            action=action,
        ),
        action_kind="shell_run",
        risk_level="high",
        title="Shell-Befehl ausführen?",
        summary="Test",
        session_grants=(SessionGrantScope(classification, "shell_action", action),),
    )


def test_shell_grant_matches_access_class_agent_and_directory(tmp_path) -> None:
    client = FakeExecutionClient({}, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, ToolCall("shell", "shell_run", {}), client)
    granted = _shell_permission(client, prefixes=[["git", "status"]])
    matching = _shell_permission(client, prefixes=[["git", "status"]])
    wrong_class = _shell_permission(client, classification="read", prefixes=[["git", "status"]])
    wrong_directory = _shell_permission(
        client, prefixes=[["git", "status"]], directory="/home/user/other"
    )
    scope = granted.effective_session_grants[0]
    grant = PermissionGrant(
        session_id=context.session.id,
        capability=scope.capability,
        resource_type=scope.resource_type,
        resource_scope=scope.resource_scope,
    )

    assert PermissionPolicy.grant_matches(grant, matching, context.root_files)
    assert PermissionPolicy.grant_matches(grant, wrong_class, context.root_files)
    assert not PermissionPolicy.grant_matches(grant, wrong_directory, context.root_files)


async def test_administrator_grants_cover_only_exact_action_and_never_disabled_rights(tmp_path) -> None:
    client = FakeExecutionClient({}, {"shell.inspect", "shell.run", "shell.classify", "administrator.run"})
    context = _context(tmp_path, ToolCall("admin", "shell_run", {}), client)
    context.session.preferred_execution_agent_id = client.agent_id
    context.db.scalar.return_value = ExecutionAgent(
        id=client.agent_id, user_id=context.session.user_id,
        home_filesystem_policy="auto", external_filesystem_policy="auto",
        home_shell_enabled=True, external_shell_enabled=True,
    )
    request = administrator_permission(client.agent_id, client.current_directory, capability="shell.read", command="cat /root/private")
    grant_scope = request.effective_session_grants[0]
    normal_grant = PermissionGrant(
        capability="resource.write", resource_type="execution_directory",
        resource_scope=execution_directory_scope(client.agent_id, client.current_directory),
    )
    exact_grant = PermissionGrant(
        capability=grant_scope.capability, resource_type=grant_scope.resource_type,
        resource_scope=grant_scope.resource_scope,
    )
    context.db.scalars.return_value = Mock(all=Mock(return_value=[normal_grant]))
    assert await PermissionPolicy.evaluate(
        context.db, context.session, request, context.root_files, ToolExecutionPolicy.ASK,
    ) == PermissionDecision.ASK
    context.db.scalars.return_value = Mock(all=Mock(return_value=[exact_grant]))
    assert await PermissionPolicy.evaluate(
        context.db, context.session, request, context.root_files, ToolExecutionPolicy.ASK,
    ) == PermissionDecision.ALLOW
    changed = administrator_permission(client.agent_id, client.current_directory, capability="filesystem.read", path="/root/other")
    assert not PermissionPolicy.grant_matches(exact_grant, changed, context.root_files)
    changed = administrator_permission(client.agent_id, client.current_directory, capability="filesystem.write", path="/root/other")
    assert not PermissionPolicy.grant_matches(exact_grant, changed, context.root_files)
    changed = administrator_permission(uuid4(), client.current_directory, capability="shell.read", command="id")
    assert not PermissionPolicy.grant_matches(exact_grant, changed, context.root_files)
    assert await PermissionPolicy.evaluate(
        context.db, context.session, request, context.root_files, ToolExecutionPolicy.DISABLED,
    ) == PermissionDecision.DENY
    context.db.scalar.return_value.external_shell_enabled = False
    request = replace(request, administrator_analysis_complete=False)
    assert await PermissionPolicy.evaluate(
        context.db, context.session, request, context.root_files, ToolExecutionPolicy.ASK,
    ) == PermissionDecision.DENY
    context.db.scalar.return_value.home_directory = "/home/user"
    context.db.scalar.return_value.home_filesystem_policy = "deny"
    file_request = administrator_permission(client.agent_id, client.current_directory, capability="filesystem.read", path="/etc/demo.conf")
    file_scope = file_request.effective_session_grants[0]
    context.db.scalars.return_value = Mock(all=Mock(return_value=[PermissionGrant(
        capability=file_scope.capability, resource_type=file_scope.resource_type, resource_scope=file_scope.resource_scope,
    )]))
    assert await PermissionPolicy.evaluate(
        context.db, context.session, file_request, context.root_files, ToolExecutionPolicy.ASK,
    ) == PermissionDecision.ALLOW


async def test_elevated_shell_uses_read_classification_and_privileged_inspection(tmp_path) -> None:
    client = FakeExecutionClient({"administrator.run": ProcessResult(
        exit_code=0, timed_out=False, duration_ms=1, stdout_bytes=0,
        stderr_bytes=0, stdout_truncated=False, stderr_truncated=False,
    )}, {"shell.run", "shell.classify", "administrator.run"})
    context = _context(tmp_path, ToolCall("admin", "shell_run", {}), client)
    result = await _shell_run(ShellRunArguments(
        command="ls /root", elevated=True,
    ), context)
    assert not result.is_error
    request = context.ask_handler.await_args.args[0]
    assert request.capability == "shell.read"
    assert request.resource_type == "execution_administrator"
    assert not request.sensitive_access


@pytest.mark.parametrize("operation", ["shell.run", "administrator.run"])
@pytest.mark.parametrize("separator", ["\n", "\r"])
@pytest.mark.parametrize("redacted_values", [(), ("Cookie",)])
async def test_process_masks_split_authentication_headers(tmp_path, monkeypatch, operation, separator, redacted_values) -> None:
    client = FakeExecutionClient({}, {operation})
    context = _context(tmp_path, ToolCall("headers", "shell_run", {}), client)
    published = AsyncMock()
    monkeypatch.setattr("mato_loop_backend.tools.computer.run_events.publish", published)

    async def execute(_operation, _arguments, _model, *, on_event, **_kwargs):
        for chunk in ("SENT Co", "okie header: session-", f"credential{separator}", f"HTTP 403{separator}"):
            await on_event(SimpleNamespace(event="stdout", data=chunk))
        return ProcessResult(
            exit_code=0, timed_out=False, duration_ms=1, stdout_bytes=52, stderr_bytes=0,
            stdout_truncated=False, stderr_truncated=False,
        )

    client.request = execute
    result = await _run_process(
        context, operation,
        {"command": "inspect"} if operation == "shell.run" else {"operation": "shell.run", "arguments": {"command": "inspect"}},
        requested_arguments={}, executed_arguments={}, redacted_values=redacted_values,
    )
    replay = terminal_coordinator._shell_outputs[(context.session.user_id, context.session.id, client.agent_id)]
    visible = result.content + str(published.await_args_list) + b"".join(item.data for item in replay.calls.values()).decode()
    assert "session-credential" not in visible and "session-" not in visible and "HTTP 403" in visible


@pytest.mark.parametrize("secret, live", [("private-password", True), ("private-\npassword", False)])
async def test_process_streams_carriage_return_progress_with_a_secret(tmp_path, monkeypatch, secret, live) -> None:
    client = FakeExecutionClient({}, {"shell.run"})
    context = _context(tmp_path, ToolCall("progress", "shell_run", {}), client)
    published = AsyncMock()
    monkeypatch.setattr("mato_loop_backend.tools.computer.run_events.publish", published)

    async def execute(_operation, _arguments, _model, *, on_event, **_kwargs):
        for chunk in ("\rProgress 25%", "\rProgress 50%") * 32:
            await on_event(SimpleNamespace(event="stderr", data=chunk))
        assert any("Progress 25%" in call.args[1]["delta"] for call in published.await_args_list) is live
        assert sum(len(json.dumps(call.args[1])) for call in published.await_args_list) < 10 * 26 * 32
        return ProcessResult(
            exit_code=0, timed_out=False, duration_ms=1, stdout_bytes=0, stderr_bytes=26 * 32,
            stdout_truncated=False, stderr_truncated=False,
        )

    client.request = execute
    await _run_process(
        context, "shell.run", {"command": "download"}, requested_arguments={},
        executed_arguments={}, redacted_values=(secret,),
    )


@pytest.mark.parametrize("text, values, expected", [
    (
        "X-Api-Key: PRIVATE_KEY\nAuthorization: Bearer PRIVATE_TOKEN\n",
        ("X-Api-Key: PRIVATE_KEY\nAuthorization: Bearer PRIVATE_TOKEN",),
        "[redacted]\n",
    ),
    (
        "Authorization: PRIVATE_KEY\nPRIVATE_TOKEN\nOK\n",
        ("PRIVATE_KEY\nPRIVATE_TOKEN",),
        "[redacted authentication header]\nOK\n",
    ),
    ("abcdef\n", ("abcd", "cdef"), "[redacted]\n"),
])
def test_process_masks_overlapping_secret_and_header_matches(text, values, expected) -> None:
    assert redact_process_output({"stdout": text}, values)["stdout"] == expected


async def test_invalid_secret_reference_reports_environment_field(tmp_path) -> None:
    client = FakeExecutionClient({}, {"shell.run"})
    call = ToolCall("login", "shell_run", {
        "command": "login", "environment": {"USERNAME": f"run-secret:{uuid4()}\\"},
    })
    result = await _tools(client.operations).execute(call, _context(tmp_path, call, client))
    assert json.loads(result.content)["error"]["field"] == "environment.USERNAME"


@pytest.mark.parametrize("mode", ["live", "secret", "truncated", "denied"])
async def test_elevated_shell_displays_command_and_safe_output_in_terminal(tmp_path, monkeypatch, mode) -> None:
    client = FakeExecutionClient({}, {"shell.run", "shell.classify", "administrator.run"})
    context = _context(
        tmp_path, ToolCall("admin", "shell_run", {}), client,
        decision=PermissionDecision.DENY if mode == "denied" else PermissionDecision.ALLOW,
    )
    terminal = TerminalRuntime(
        user_id=context.session.user_id, owner_session_id=context.session.id,
        agent_id=client.agent_id, login_id=uuid4(), terminal_id=str(uuid4()),
        client=client, generation=1,
    )
    terminal.viewer = BrowserTerminalConnection(terminal, Mock())
    monkeypatch.setitem(terminal_coordinator._runtimes, (
        context.session.user_id, context.session.id, client.agent_id,
    ), terminal)
    protected = mode in {"secret", "truncated"}
    password_id = uuid4()
    arguments = ShellRunArguments(command="id -u", elevated=True)
    if protected:
        context.one_time_secrets[password_id] = "private-password"
        arguments.sudo_password_ref = f"run-secret:{password_id}"

    async def execute(operation, _arguments, _model, *, on_event=None, **_kwargs):
        if operation == "administrator.run" and _arguments["operation"] == "authenticate":
            return AdministratorAuthenticationResult(passwordless=True)
        if operation == "shell.classify" or _arguments["operation"] == "shell.inspect":
            return ShellInspectResult(required_categories=["read"], classification="read", read_only=True, analysis_complete=True, inspection_revision="a" * 64, targets=[], commands=[])
        assert operation == "administrator.run"
        output = next(iter(terminal.shell_output.calls.values()))
        assert output.data == b"\r\nsudo id -u\r\n"
        chunks = [("stdout", "0\n"), ("stderr", "warning\r\n")]
        if protected:
            chunks = [("stdout", "private-"), ("stdout", "password\n"), ("stderr", "warning\n")]
        if mode == "truncated":
            chunks.append(("stdout", "private-"))
        for index, (channel, data) in enumerate(chunks):
            await on_event(SimpleNamespace(event=channel, data=data))
            if protected and index == 0:
                assert output.data == b"\r\nsudo id -u\r\n"
            elif mode == "live":
                assert output.data.endswith(data.replace("\r\n", "\n").replace("\n", "\r\n").encode())
        return ProcessResult(
            exit_code=1, timed_out=False, duration_ms=1, stdout_bytes=17 if protected else 2,
            stderr_bytes=8, stdout_truncated=mode == "truncated", stderr_truncated=False,
        )

    request = AsyncMock(side_effect=execute)
    monkeypatch.setattr(client, "request", request)
    result = await _shell_run(arguments, context)
    assert result.is_error
    if mode == "denied":
        assert terminal.personal_buffer == b""
    else:
        expected = {
            "live": b"0\r\nwarning\r\n",
            "secret": b"[redacted]\r\nwarning\r\n",
            "truncated": b"[redacted]\r\nwarning\r\n[redacted][redacted]",
        }[mode]
        output = terminal.shell_output.calls[result.metadata["shellExecution"]["id"]]
        assert output.data == b"\r\nsudo id -u\r\n" + expected
        messages = [json.loads(terminal.viewer.queue.get_nowait()[0]) for _ in range(terminal.viewer.queue.qsize())]
        delivered = b"".join(base64.b64decode(item["data"]) for item in messages if item["type"] == "terminal.agentOutput")
        assert delivered == output.data
        assert terminal.personal_buffer == b""
        assert b"private-password" not in delivered
    if mode == "denied":
        assert request.await_count == 1  # Classification never executes a command.


async def test_password_reference_does_not_grant_access_to_another_computer(tmp_path) -> None:
    response = ProcessResult(
        exit_code=0, timed_out=False, duration_ms=1, stdout_bytes=0,
        stderr_bytes=0, stdout_truncated=False, stderr_truncated=False,
    )
    client = FakeExecutionClient({"administrator.run": response}, {"shell.run", "shell.classify", "administrator.run"})
    context = _context(tmp_path, ToolCall("admin", "shell_run", {}), client)
    name = f"SUDO_PASSWORD_{client.agent_id.hex.upper()}"
    references = session_secrets.put(context.session.id, {name: "private password"})
    try:
        result = await _shell_run(ShellRunArguments(command="id -u", elevated=True), context)
        assert not result.is_error
        assert client.requests[-1] == (
            "administrator.run",
            {"operation": "shell.run", "arguments": {"command": "id -u", "currentDirectory": client.current_directory,
             "expectedInspectionRevision": "a" * 64}, "sudoPassword": "private password"},
        )
        assert "private password" not in result.content
        client.agent_id = uuid4()
        context.ask_handler.return_value = PermissionDecision.ASK
        result = await _shell_run(ShellRunArguments(
            command="id -u", elevated=True, sudo_password_ref=references[name],
        ), context)
        assert result.is_error
        assert json.loads(result.content)["error"]["code"] == "permission.denied"
    finally:
        session_secrets.clear_session(context.session.id)


async def test_elevated_shell_resolves_run_secrets_and_forwards_explicit_environment(tmp_path) -> None:
    response = ProcessResult(
        exit_code=0, timed_out=False, duration_ms=1, stdout_bytes=0,
        stderr_bytes=0, stdout_truncated=False, stderr_truncated=False,
    )
    client = FakeExecutionClient({"administrator.run": response}, {"shell.run", "shell.classify", "administrator.run"})
    context = _context(tmp_path, ToolCall("admin", "shell_run", {}), client)
    sudo_name = f"SUDO_PASSWORD_{client.agent_id.hex.upper()}"
    password_id, token_id = uuid4(), uuid4()
    context.one_time_secrets.update({password_id: "sudo-private", token_id: "env-private"})
    context.run_secret_refs[sudo_name] = f"run-secret:{password_id}"

    result = await _shell_run(ShellRunArguments(
        command="test -n \"$APP_TOKEN\"", elevated=True,
        environment={"APP_TOKEN": f"run-secret:{token_id}"},
    ), context)

    assert not result.is_error
    assert client.requests[-1] == (
        "administrator.run",
        {"operation": "shell.run", "arguments": {"command": 'test -n "$APP_TOKEN"', "currentDirectory": client.current_directory,
         "expectedInspectionRevision": "a" * 64, "environment": {"APP_TOKEN": "env-private"}}, "sudoPassword": "sudo-private"},
    )
    assert "env-private" not in result.content
    assert "sudo-private" not in result.content


@pytest.mark.parametrize("remember", [False, True])
@pytest.mark.parametrize("error_code", ["process.sudoAuthFailed", "process.timedOut"])
async def test_elevated_shell_reprompts_only_for_rejected_passwords(tmp_path, remember, error_code) -> None:
    def fail(_arguments):
        raise ExecutionClientError(error_code)

    client = FakeExecutionClient({"administrator.run": fail}, {"shell.run", "shell.classify", "administrator.run"})
    context = _context(tmp_path, ToolCall("admin", "shell_run", {}), client)
    name = "SERVER_PASSWORD"
    other_name = f"SUDO_PASSWORD_{uuid4().hex.upper()}"
    other_refs = session_secrets.put(context.session.id, {other_name: "other password"})
    question = AsyncMock(side_effect=[
        ProtectedQuestionResolution({name: ["wrong"]}, frozenset({name}) if remember else frozenset()),
        ProtectedQuestionResolution({name: ["correct"]}, frozenset()),
    ])
    context.question_handler = question
    secret = ToolCall("password", "secret_input", {"inputs": [
        {"name": name, "label": "sudo", "question": "Enter the password"},
    ]})
    shell = ToolCall("admin", "shell_run", {"command": "id -u", "elevated": True})
    tools = _tools(client.operations)
    try:
        context.tool_call = secret
        original = await tools.execute(secret, context)
        shell.arguments["sudo_password_ref"] = json.loads(original.content)["environment"][name]
        context.tool_call = shell
        failed = await tools.execute(shell, context)
        assert json.loads(failed.content)["error"]["code"] == error_code
        assert client.requests[-1][1]["sudoPassword"] == "wrong"

        context.tool_call = secret
        replacement = await tools.execute(secret, context)
        rejected = error_code == "process.sudoAuthFailed"
        assert question.await_count == (2 if rejected else 1)
        assert (replacement.content != original.content) is rejected
        assert session_secrets.references(context.session.id)[other_name] == other_refs[other_name]

        client.responses["administrator.run"] = ProcessResult(
            exit_code=0, timed_out=False, duration_ms=1, stdout_bytes=0,
            stderr_bytes=0, stdout_truncated=False, stderr_truncated=False,
        )
        context.tool_call = shell
        shell.arguments["sudo_password_ref"] = json.loads(replacement.content)["environment"][name]
        result = await tools.execute(shell, context)
        assert not result.is_error
        assert client.requests[-1][1]["sudoPassword"] == ("correct" if rejected else "wrong")
    finally:
        session_secrets.clear_session(context.session.id)


async def test_elevated_shell_requires_a_capable_agent_before_approval(tmp_path) -> None:
    client = FakeExecutionClient({}, {"shell.run", "shell.inspect"})
    context = _context(tmp_path, ToolCall("admin", "shell_run", {}), client)
    result = await _shell_run(ShellRunArguments(command="id", elevated=True), context)
    assert json.loads(result.content)["error"]["code"] == "executionAgent.operationUnavailable"
    context.ask_handler.assert_not_awaited()


@pytest.mark.parametrize("remember", [False, True])
async def test_administrator_input_authenticates_without_granting_access(tmp_path, monkeypatch, remember):
    client = FakeExecutionClient({"administrator.run": ProcessResult(
        exit_code=0, timed_out=False, duration_ms=1, stdout_bytes=0,
        stderr_bytes=0, stdout_truncated=False, stderr_truncated=False,
    )}, {"shell.run", "shell.classify", "administrator.run"})
    call = ToolCall("password", "secret_input", {"inputs": [{
        "name": "SERVER_PASSWORD", "label": "Administrator", "question": "Enter the password",
        "administrator": True,
    }]})
    context = _context(tmp_path, call, client)
    context.session.preferred_execution_agent_id = client.agent_id
    context.agent_profile_id = uuid4()
    agent = ExecutionAgent(
        id=client.agent_id, alias="AI-PC", user_id=context.session.user_id,
        home_filesystem_policy="auto", external_filesystem_policy="auto",
        home_shell_enabled=True, external_shell_enabled=True,
    )
    context.db.scalar.side_effect = [agent, None, None] if remember else [agent]
    context.db.add = Mock()
    run = AgentRun(
        id=context.run_id, session_id=context.session.id,
        execution_agent_id=client.agent_id, agent_profile_id=context.agent_profile_id, trust_level="public",
    )
    context.db.get.side_effect = lambda model, _id: run if model is AgentRun else context.session
    resolved = Mock(return_value=True)
    monkeypatch.setattr(AgentRuntime, "claim_question_resolution", Mock(return_value=True))
    monkeypatch.setattr(AgentRuntime, "resolve_question", resolved)
    monkeypatch.setattr("mato_loop_backend.agent.question_decisions.run_events.publish", AsyncMock())

    async def submit(questions):
        question = QuestionRequest(
            id=uuid4(), session_id=context.session.id, run_id=context.run_id, status="pending",
            questions=questions.model_dump(mode="json")["questions"],
        )
        await answer_question(context.db, question, {"SERVER_PASSWORD": ["private password"]},
                              remember_for_session=["SERVER_PASSWORD"] if remember else [])
        assert question.answers == {"SERVER_PASSWORD": ["received"]}
        return resolved.call_args.args[1]

    context.question_handler = AsyncMock(side_effect=submit)
    try:
        result = await _tools(client.operations).execute(call, context)
        reference = json.loads(result.content)["environment"]["SERVER_PASSWORD"]
        target = context.question_handler.await_args.args[0].questions[0].administrator
        assert (target.agent_id, target.alias) == (client.agent_id, "AI-PC")
        grants = [item.args[0] for item in context.db.add.call_args_list]
        assert grants == []
        context.db.scalar.side_effect = None
        context.db.scalar.return_value = agent
        context.db.scalars.return_value = Mock(all=Mock(side_effect=lambda: grants))

        async def permission(request, *, check_only=False):
            decision = await PermissionPolicy.evaluate(
                context.db, context.session, request, context.root_files, ToolExecutionPolicy.ASK,
                agent_profile_id=context.agent_profile_id, trust_level=context.model_trust_level,
            )
            return decision

        context.ask_handler = AsyncMock(side_effect=permission)
        shell = ShellRunArguments(command="id -u", elevated=True, sudo_password_ref=reference)
        assert (await _shell_run(shell, context)).is_error
    finally:
        session_secrets.clear_session(context.session.id)


@pytest.mark.parametrize("sensitive_policy", list(ToolExecutionPolicy))
async def test_administrator_authentication_preserves_action_policy(tmp_path, monkeypatch, sensitive_policy):
    from mato_loop_backend.tools.administrator import authorize

    client = FakeExecutionClient({}, {"administrator.run"})
    context = _context(tmp_path, ToolCall("read", "read", {}), client, decision=(
        PermissionDecision.DENY if sensitive_policy == ToolExecutionPolicy.DISABLED else PermissionDecision.ASK
    ))
    context.tool_settings.permissions.policies["sensitive_data.access"] = sensitive_policy
    monkeypatch.setattr(
        "mato_loop_backend.tools.access.profile_tool_settings", AsyncMock(return_value=context.tool_settings),
    )
    allowed = await authorize(context, administrator_permission(
        client.agent_id, client.current_directory, capability="filesystem.read", path="/root/.env",
    ))
    assert (allowed, context.ask_handler.await_count) == (
        False, 1 if sensitive_policy == ToolExecutionPolicy.DISABLED else 2,
    )


@pytest.mark.parametrize("mode", ["allowed", "secret_denied", "admin_denied", "explicit", "no_longer_secret"])
async def test_computer_sudo_secret_uses_existing_secret_and_action_permissions(tmp_path, monkeypatch, mode):
    client = FakeExecutionClient({"administrator.run": ProcessResult(
        exit_code=0, timed_out=False, duration_ms=1, stdout_bytes=0, stderr_bytes=0,
        stdout_truncated=False, stderr_truncated=False,
    )}, {"administrator.run", "shell.classify"})
    context = _context(tmp_path, ToolCall("admin", "shell_run", {}), client)
    credential_id = uuid4()
    context.db.get.return_value = ExecutionAgent(
        id=client.agent_id, user_id=context.session.user_id, sudo_credential_id=credential_id,
    )
    variable = Mock(id=credential_id, label="COMPUTER_SUDO", metadata_json={"secret": mode != "no_longer_secret"})
    monkeypatch.setattr("mato_loop_backend.tools.secrets.get_personal_variable", AsyncMock(return_value=variable))
    decode = Mock(return_value="stored password")
    monkeypatch.setattr("mato_loop_backend.tools.secrets.personal_variable_value", decode)
    monkeypatch.setattr("mato_loop_backend.tools.access.profile_tool_settings", AsyncMock(return_value=context.tool_settings))

    async def authorize(request, **kwargs):
        denied = (mode == "secret_denied" and request.capability == "secrets.access") or (
            mode == "admin_denied" and request.resource_type == "execution_administrator"
        )
        return PermissionDecision.DENY if denied else PermissionDecision.ALLOW

    context.ask_handler = AsyncMock(side_effect=authorize)
    arguments = ShellRunArguments(command="id -u", elevated=True)
    if mode == "explicit":
        identifier = uuid4()
        context.one_time_secrets[identifier] = "one-time password"
        arguments.sudo_password_ref = f"run-secret:{identifier}"
    result = await _shell_run(arguments, context)
    executions = [args for op, args in client.requests if op == "administrator.run"]
    if mode in {"secret_denied", "admin_denied", "no_longer_secret"}:
        assert result.is_error and executions == []
        decode.assert_not_called()
    else:
        assert executions[-1]["sudoPassword"] == ("one-time password" if mode == "explicit" else "stored password")
        assert "stored password" not in result.content


async def test_secret_input_reports_skipped_administrator_password_without_grant(tmp_path):
    client = FakeExecutionClient({}, {"administrator.run"})
    call = ToolCall("secret", "secret_input", {"inputs": [
        {"name": "SSH_USERNAME", "label": "Username", "question": "Enter username", "masked": False},
        {"name": "SSH_PASSWORD", "label": "Password", "question": "Enter password", "administrator": True},
    ]})
    context = _context(tmp_path, call, client)
    context.session.preferred_execution_agent_id = client.agent_id
    context.db.scalar.return_value = ExecutionAgent(id=client.agent_id, alias="AI-PC")
    context.question_handler = AsyncMock(return_value=ProtectedQuestionResolution(
        {"SSH_USERNAME": ["user"], "SSH_PASSWORD": []},
    ))

    result = json.loads((await _tools(client.operations).execute(call, context)).content)

    assert result["unanswered"] == ["SSH_PASSWORD"]
    assert set(result["environment"]) == {"SSH_USERNAME"}


async def test_variable_write_reports_skipped_value(tmp_path, monkeypatch):
    client = FakeExecutionClient({}, set())
    call = ToolCall("save", "variable_write", {"name": "API_TOKEN", "secret": True})
    context = _context(tmp_path, call, client)
    context.question_handler = AsyncMock(return_value={"value": []})
    monkeypatch.setattr(mock_tools_interaction.tools_access, "profile_tool_settings", AsyncMock(return_value=ToolSettings()))

    result = await tool_registry.execute(call, context)

    assert json.loads(result.content) == {"saved": False, "name": "API_TOKEN", "unanswered": ["value"]}


@pytest.mark.parametrize("remember", [False, True])
async def test_missing_administrator_password_is_collected_and_reused_without_model_turn(tmp_path, remember):
    client = FakeExecutionClient(
        {"authenticate": AdministratorAuthenticationResult(passwordless=False), "administrator.run": ProcessResult(
            exit_code=0, timed_out=False, duration_ms=1, stdout_bytes=0, stderr_bytes=0,
            stdout_truncated=False, stderr_truncated=False,
        )},
        {"shell.run", "shell.classify", "administrator.run"},
    )
    call = ToolCall("admin", "shell_run", {"command": "id -u", "elevated": True})
    context = _context(tmp_path, call, client)
    context.session.preferred_execution_agent_id = client.agent_id
    agent = ExecutionAgent(id=client.agent_id, user_id=context.session.user_id, alias="AI-PC")
    context.db.scalar.return_value = agent
    context.db.get.side_effect = lambda model, *args, **kwargs: AgentRun(id=context.run_id) if model is AgentRun else agent
    name = f"SUDO_PASSWORD_{client.agent_id.hex.upper()}"
    context.question_handler = AsyncMock(return_value=ProtectedQuestionResolution(
        {name: ["private password"]}, frozenset({name}) if remember else frozenset(),
    ))
    try:
        for _ in range(2):
            result = await _tools(client.operations).execute(call, context)
            assert not result.is_error
        assert context.question_handler.await_count == 1
        assert client.requests[-1][1]["sudoPassword"] == "private password"
        assert "private password" not in result.content
    finally:
        session_secrets.clear_session(context.session.id)


@pytest.mark.parametrize("mode", ["skipped", "rejected", "expired", "denied", "policy_changed", "target_changed"])
async def test_missing_administrator_password_never_bypasses_cancellation_or_policy(tmp_path, mode):
    client = FakeExecutionClient(
        {"authenticate": AdministratorAuthenticationResult(passwordless=False)},
        {"shell.run", "shell.classify", "administrator.run"},
    )
    call = ToolCall("admin", "shell_run", {"command": "id -u", "elevated": True})
    context = _context(tmp_path, call, client, decision=(
        PermissionDecision.DENY if mode == "denied" else PermissionDecision.ALLOW
    ))
    context.session.preferred_execution_agent_id = client.agent_id
    context.db.scalar.return_value = ExecutionAgent(id=client.agent_id, alias="AI-PC")

    async def answer(questions):
        if mode == "policy_changed":
            context.ask_handler.return_value = PermissionDecision.DENY
        if mode == "target_changed":
            context.session.preferred_execution_agent_id = uuid4()
        return {"rejected": "rejected", "expired": None}.get(mode, ProtectedQuestionResolution({
            questions.questions[0].id: [] if mode == "skipped" else ["private password"],
        }))

    context.question_handler = AsyncMock(side_effect=answer)
    context.db.get.side_effect = lambda model, *args, **kwargs: AgentRun(id=context.run_id) if model is AgentRun else None
    result = await _tools(client.operations).execute(call, context)
    assert result.metadata["errorCode"] == {
        "skipped": "question.rejected", "rejected": "question.rejected", "expired": "question.expired",
    }.get(mode, "permission.denied")
    assert context.user_denied is (mode in {"skipped", "rejected"})
    assert not any(
        operation == "administrator.run" and arguments["operation"] != "authenticate"
        for operation, arguments in client.requests
    )
    if mode == "denied":
        context.question_handler.assert_not_awaited()


def test_shell_session_grants_bind_categories_to_the_command() -> None:
    agent_id = uuid4()
    requests = shell_command_permissions(
        agent_id,
        "/work",
        "cat /outside/input.txt > /other/output.txt",
        ShellInspectResult(
            required_categories=["read", "write"],
            targets=[
                {"path": "/outside/input.txt", "kind": "input", "access": "read"},
                {"path": "/other/output.txt", "kind": "output", "access": "write"},
                {"path": "/generic/file.txt", "kind": "path", "access": "write"},
            ],
            analysis_complete=True,
            classification="write",
            read_only=False,
            commands=[],
            inspection_revision="a" * 64,
        ),
    )

    assert [(request.capability, request.effective_session_grants[0].resource_type,
             request.effective_session_grants[0].resource_scope) for request in requests] == [
        (f"shell.{category}", "shell_action", shell_action_scope(agent_id, "/work", "cat /outside/input.txt > /other/output.txt"))
        for category in ("write", "read")
    ]
    request = next(request for request in requests if request.capability == "shell.write")
    assert all(
        session_grant_matches_request(
            request.capability, request.resource_type, request.resource_scope, grant
        )
        for request in requests for grant in request.effective_session_grants
    )
    outside_read = request.effective_session_grants[0]
    assert not session_grant_matches_request(
        request.capability,
        request.resource_type,
        request.resource_scope,
        replace(outside_read, access="read"),
    )


def test_shell_permission_displays_confidential_paths() -> None:
    request, = shell_command_permissions(
        uuid4(),
        "/work",
        "cat .env.prod.example README.md",
        ShellInspectResult(
            required_categories=["read"],
            targets=[
                {"path": "/work/.env.prod.example", "kind": "path", "access": "read"},
                {"path": "/work/README.md", "kind": "path", "access": "read"},
            ],
            analysis_complete=True,
            classification="read",
            read_only=True,
            commands=[],
            inspection_revision="a" * 64,
        ),
    )

    assert request.display_items[-1] == PermissionDisplayItem(
        "Besonders geschützte Pfade", ("/work/.env.prod.example",), "list"
    )
    assert "Richtlinie für vertrauliche Daten" in request.summary
    assert request.sensitive_access


@pytest.mark.parametrize("blocked", [None, "delete", "revoked_write"])
async def test_mixed_shell_categories_are_all_checked_before_execution(tmp_path, blocked):
    client = FakeExecutionClient({}, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, ToolCall("shell", "shell_run", {}), client)
    requests = shell_command_permissions(client.agent_id, client.current_directory, "touch new; rm old", ShellInspectResult(
        required_categories=["write", "delete"], classification="write", read_only=False,
        targets=[], commands=[], analysis_complete=True, inspection_revision="a" * 64,
    ))
    allowed = set()

    async def authorize(request, *, check_only=False):
        if request.capability == "shell.delete" and blocked == "delete":
            return PermissionDecision.DENY
        if blocked == "revoked_write" and "shell.delete" in allowed and request.capability == "shell.write":
            return PermissionDecision.DENY
        if not check_only:
            allowed.add(request.capability)
        return PermissionDecision.ALLOW

    context.ask_handler = AsyncMock(side_effect=authorize)
    assert await _authorize_shell(context, requests) is (blocked is None)


@pytest.mark.parametrize("prepare", [False, True])
async def test_mixed_read_write_shell_needs_only_the_write_approval(tmp_path, prepare):
    from mato_loop_backend.tools.permissions import authorize_session_requests

    client = FakeExecutionClient({}, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, ToolCall("shell", "shell_run", {}), client)
    requests = shell_command_permissions(client.agent_id, client.current_directory, "cat input > output", ShellInspectResult(
        required_categories=["read", "write"], classification="write", read_only=False,
        targets=[], commands=[], analysis_complete=True, inspection_revision="a" * 64,
    ))
    grants, prompts = [], []

    async def authorize(request, *, check_only=False):
        if any(PermissionPolicy.grant_matches(grant, request, context.root_files) for grant in grants):
            return PermissionDecision.ALLOW
        if check_only:
            return PermissionDecision.ASK
        prompts.append(request.capability)
        scope = request.effective_session_grants[0]
        grants.append(PermissionGrant(capability=scope.capability, resource_type=scope.resource_type, resource_scope=scope.resource_scope))
        return PermissionDecision.ALLOW

    context.ask_handler = authorize
    allowed = not (await authorize_session_requests(context, requests)).is_error if prepare else await _authorize_shell(context, requests)
    assert (allowed, prompts) == (True, ["shell.write"])


async def test_edited_mixed_command_is_approved_once_through_runtime(tmp_path, monkeypatch):
    edited = "cat input > new-output"
    call = ToolCall("shell", "shell_run", {"command": "cat input > old-output"})
    client = FakeExecutionClient({
        "shell.inspect": ShellInspectResult(required_categories=["read", "write"], classification="write",
            targets=[], commands=[], analysis_complete=True, inspection_revision="a" * 64),
        "shell.run": ProcessResult(exit_code=0, timed_out=False, duration_ms=1, stdout_bytes=0,
            stderr_bytes=0, stdout_truncated=False, stderr_truncated=False),
    }, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, call, client)
    run = AgentRun(id=context.run_id, session_id=context.session.id, trust_level="public")
    agent = ExecutionAgent(id=client.agent_id, user_id=context.session.user_id, home_directory="/home/user",
        home_filesystem_policy="auto", external_filesystem_policy="auto", home_shell_enabled=True, external_shell_enabled=True)
    settings = ToolSettings(public_permissions={"policies": {"shell.read": "auto", "shell.write": "ask"}}).select_trust(TrustLevel.PUBLIC)
    monkeypatch.setattr("mato_loop_backend.agent.runtime.load_agent_profile_tools", AsyncMock(return_value=(settings, sysadmin_profile())))
    monkeypatch.setattr("mato_loop_backend.agent.runtime.load_effective_integrations", AsyncMock(return_value=IntegrationSettings()))
    approve = AsyncMock(side_effect=[ApprovalResolution("allow_once", argument_edits=((('command',), edited),))])
    monkeypatch.setattr(AgentRuntime, "_request_approval", approve)
    engine = create_engine("sqlite://")
    PermissionGrant.__table__.create(engine)
    try:
        with Session(engine) as stored:
            context.db.scalar.side_effect = lambda statement: agent if statement.column_descriptions[0]["entity"] is ExecutionAgent else stored.scalar(statement)
            context.db.scalars.side_effect = stored.scalars
            context.db.add = Mock(side_effect=stored.add)
            context.db.commit.side_effect = stored.commit

            async def authorize(request, **kwargs):
                return await AgentRuntime._authorize(context.db, run, context.session, call, request,
                    context.root_files, context.approval_resolution, **kwargs)

            context.ask_handler = authorize
            result = await _tools(client.operations).execute(call, context)
            assert (result.is_error, approve.await_count, client.requests[-1][0], client.requests[-1][1]["command"]) == (
                False, 1, "shell.run", edited,
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(("command_line", "tokens", "targets"), [
    ("cat password.txt", ["cat", "password.txt"], [
        {"path": "/work/password.txt", "kind": "path", "access": "read"},
    ]),
    ("cat secrets/production.yaml", ["cat", "secrets/production.yaml"], [
        {"path": "/work/secrets/production.yaml", "kind": "path", "access": "read"},
    ]),
    ("printenv API_TOKEN", ["printenv", "API_TOKEN"], []),
    ('echo "$DB_PASSWORD"', ["echo", "$DB_PASSWORD"], []),
    (
        "printf '%s\\n' \"$SUDO_PASSWORD\" | sudo -S true",
        ["printf", "%s\\n", "$SUDO_PASSWORD"],
        [],
    ),
    ("env", ["env"], []),
    ("export -p", ["export", "-p"], []),
])
def test_shell_permission_marks_possible_secret_output(
    command_line, tokens, targets,
) -> None:
    request, = shell_command_permissions(
        uuid4(),
        "/work",
        command_line,
        ShellInspectResult(
            required_categories=["read"],
            targets=targets,
            analysis_complete=True,
            classification="read",
            read_only=True,
            commands=[{"tokens": tokens, "prefix": [tokens[0]], "classification": "read"}],
            inspection_revision="a" * 64,
        ),
    )

    assert request.sensitive_access
    assert request.display_items[-1].label == "Mögliche Secret-Ausgabe"
    assert "nicht sicher unterscheiden" in request.display_items[-1].value


@pytest.mark.parametrize(("command_line", "tokens"), [
    ("cat README.md", ["cat", "README.md"]),
    ("printenv PATH", ["printenv", "PATH"]),
    ('echo "$HOME"', ["echo", "$HOME"]),
    ("git status", ["git", "status"]),
])
def test_shell_permission_does_not_flag_ordinary_output(command_line, tokens) -> None:
    request, = shell_command_permissions(
        uuid4(),
        "/work",
        command_line,
        ShellInspectResult(
            required_categories=["read"],
            targets=[],
            analysis_complete=True,
            classification="read",
            read_only=True,
            commands=[{"tokens": tokens, "prefix": [tokens[0]], "classification": "read"}],
            inspection_revision="a" * 64,
        ),
    )

    assert not request.sensitive_access


async def test_possible_secret_output_requires_approval_under_auto_policy(tmp_path) -> None:
    client = FakeExecutionClient({}, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, ToolCall("shell", "shell_run", {}), client)
    context.session.current_directory = "/work"
    context.db.scalar.return_value = ExecutionAgent(
        id=client.agent_id,
        user_id=context.session.user_id,
        home_directory="/work",
        home_filesystem_policy="auto",
        external_filesystem_policy="ask",
        home_shell_enabled=True,
        external_shell_enabled=True,
    )
    request, = shell_command_permissions(
        client.agent_id,
        "/work",
        'echo "$DB_PASSWORD"',
        ShellInspectResult(
            required_categories=["read"],
            targets=[],
            analysis_complete=False,
            classification="read",
            read_only=True,
            commands=[{
                "tokens": ["echo", "$DB_PASSWORD"],
                "classification": "read",
            }],
            inspection_revision="a" * 64,
        ),
    )

    assert await PermissionPolicy.evaluate(
        context.db, context.session, request, context.root_files, ToolExecutionPolicy.AUTO,
    ) == PermissionDecision.ASK
    context.db.scalars.assert_not_awaited()


def test_legacy_shell_targets_can_use_exact_command_consent() -> None:
    agent_id = uuid4()
    request, = shell_command_permissions(
        agent_id,
        "/work",
        "cat /outside/input.txt",
        ShellInspectResult(
            required_categories=["read"],
            targets=[{"path": "/outside/input.txt", "kind": "input"}],
            analysis_complete=True, classification="read", read_only=True,
            commands=[], inspection_revision="a" * 64,
        ),
    )
    assert request.available_decisions == ("allow_once", "allow_session", "deny")
    assert json.loads(request.resource_scope)["targetAccesses"] == [None]


async def test_legacy_shell_target_is_not_authorized_by_existing_directory_grant(tmp_path) -> None:
    client = FakeExecutionClient({}, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, ToolCall("shell", "shell_run", {}), client)
    context.session.current_directory = "/work"
    request, = shell_command_permissions(
        client.agent_id,
        "/work",
        "cat /outside/input.txt",
        ShellInspectResult(
            required_categories=["read"],
            targets=[{"path": "/outside/input.txt", "kind": "input"}],
            analysis_complete=True, classification="read", read_only=True,
            commands=[], inspection_revision="a" * 64,
        ),
    )
    context.db.scalar.return_value = ExecutionAgent(
        id=client.agent_id, user_id=context.session.user_id,
        home_filesystem_policy="ask", external_filesystem_policy="ask",
        home_shell_enabled=True, external_shell_enabled=True,
    )
    context.db.scalars.return_value = Mock(all=Mock(return_value=[PermissionGrant(
        session_id=context.session.id,
        capability="resource.read",
        resource_type="execution_directory",
        resource_scope=execution_directory_scope(client.agent_id, "/work"),
    )]))

    assert await PermissionPolicy.evaluate(
        context.db, context.session, request, context.root_files, ToolExecutionPolicy.ASK,
    ) == PermissionDecision.ASK


def test_configured_shell_prefixes_are_class_specific(tmp_path) -> None:
    client = FakeExecutionClient({}, {"shell.inspect", "shell.run"})
    settings = ToolSettings(shell_prefixes={"read": [["cat"]], "write": [["git"]]})

    assert shell_prefix_allowed(
        settings, _shell_permission(client, prefixes=[["git", "status"]])
    )
    assert not shell_prefix_allowed(
        settings,
        _shell_permission(client, classification="read", prefixes=[["git", "status"]]),
    )
    request = _shell_permission(client, prefixes=[["git", "status"]])
    assert _tool_policy(settings, IntegrationSettings(), request) == ToolExecutionPolicy.AUTO
    settings.permissions.policies["shell.write"] = ToolExecutionPolicy.DISABLED
    assert _tool_policy(settings, IntegrationSettings(), request) == ToolExecutionPolicy.DISABLED


async def test_disabled_shell_zone_is_a_hard_boundary(tmp_path) -> None:
    call = ToolCall("shell", "shell_run", {"command": "cat README.md"})
    client = FakeExecutionClient({}, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, call, client)
    context.db.scalar.return_value = ExecutionAgent(
        id=client.agent_id,
        user_id=context.session.user_id,
        alias="Computer",
        home_directory="/home/user",
        home_filesystem_policy="auto",
        external_filesystem_policy="ask",
        home_shell_enabled=False,
    )

    decision = await PermissionPolicy.evaluate(
        context.db,
        context.session,
        _shell_permission(client, classification="read", prefixes=[["cat"]]),
        context.root_files,
        policy="auto",
    )

    assert decision == PermissionDecision.DENY
    statement = context.db.scalar.await_args.args[0]
    assert statement.get_execution_options()["populate_existing"] is True


@pytest.mark.parametrize("profile_factory", [plan_profile, build_profile])
@pytest.mark.parametrize("capability", ["filesystem.read", "shell.read"])
@pytest.mark.parametrize(
    ("path", "filesystem_policy", "expected"),
    [
        ("/home/user/project/README.md", "auto", PermissionDecision.ALLOW),
        ("/opt/lemonade/docker-compose.yaml", "auto", PermissionDecision.ASK),
        ("/home/user/project/.env", "auto", PermissionDecision.ASK),
        ("/home/user/project/README.md", "deny", PermissionDecision.DENY),
    ],
)
async def test_coding_profile_auto_reads_keep_path_boundaries(
    tmp_path, profile_factory, capability, path, filesystem_policy, expected
) -> None:
    client = FakeExecutionClient({}, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, ToolCall("read", "read", {}), client)
    context.db.scalar.return_value = ExecutionAgent(
        id=client.agent_id,
        user_id=context.session.user_id,
        home_directory="/home/user",
        home_filesystem_policy=filesystem_policy,
        external_filesystem_policy="ask",
        home_shell_enabled=True,
        external_shell_enabled=True,
    )
    context.db.scalars.return_value = Mock(all=Mock(return_value=[]))
    profile = profile_factory()
    settings = ToolSettings().select_trust(TrustLevel.PUBLIC)
    settings._agent_permissions = profile.public_permissions
    if capability == "shell.read":
        request = _shell_permission(
            client, classification="read", prefixes=[["cat"]], paths=[path]
        )
    else:
        request = execution_directory_permission(client.agent_id, path, capability=capability)
    policy = _tool_policy(settings, IntegrationSettings(), request)

    assert policy == ToolExecutionPolicy.AUTO
    if capability == "shell.read" and path == "/opt/lemonade/docker-compose.yaml":
        expected = PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        context.db, context.session, request, context.root_files, policy,
        agent_profile_id=profile.id, trust_level=TrustLevel.PUBLIC,
    ) == expected


@pytest.mark.parametrize("trust", [TrustLevel.PRIVATE, TrustLevel.PUBLIC])
@pytest.mark.parametrize("capability", ["filesystem.read", "shell.read"])
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/srv/stacks/sonobin/compose.yaml", PermissionDecision.ALLOW),
        ("/srv/stacks/sonobin/.env", PermissionDecision.ASK),
    ],
)
async def test_sysadmin_external_reads_only_ask_for_protected_paths(
    tmp_path, trust, capability, path, expected
) -> None:
    client = FakeExecutionClient({}, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, ToolCall("read", "read", {}), client)
    context.db.scalar.return_value = ExecutionAgent(
        id=client.agent_id,
        user_id=context.session.user_id,
        home_directory="/home/user",
        home_filesystem_policy="auto",
        external_filesystem_policy="ask",
        home_shell_enabled=True,
        external_shell_enabled=True,
    )
    profile = sysadmin_profile()
    settings = ToolSettings().select_trust(trust)
    settings._agent_permissions = (
        profile.private_permissions if trust == TrustLevel.PRIVATE else profile.public_permissions
    )
    request = (
        execution_directory_permission(client.agent_id, path, capability=capability)
        if capability == "filesystem.read"
        else shell_command_permissions(
            client.agent_id,
            client.current_directory,
            f"cat {path}",
            ShellInspectResult(
                required_categories=["read"],
                targets=[{"path": path, "kind": "input", "access": "read"}],
                analysis_complete=True,
                classification="read",
                read_only=True,
                commands=[],
                inspection_revision="a" * 64,
            ),
        )[0]
    )

    policy = _tool_policy(settings, IntegrationSettings(), request)
    assert policy == ToolExecutionPolicy.AUTO
    assert await PermissionPolicy.evaluate(
        context.db, context.session, request, context.root_files, policy,
        external_working_directory_policy=_external_working_directory_policy(settings),
    ) == expected


@pytest.mark.parametrize("profile_factory", [plan_profile, build_profile, None])
@pytest.mark.parametrize("trust", [TrustLevel.PUBLIC, TrustLevel.PRIVATE])
@pytest.mark.parametrize("classification", ["read", "write"])
@pytest.mark.parametrize(
    ("analysis_complete", "path", "agent_overrides", "expected"),
    [
        (True, "/workspace/harness-eval-astra/app/frontend", {}, PermissionDecision.ALLOW),
        (False, "/workspace/harness-eval-astra/app/frontend", {}, PermissionDecision.ALLOW),
        (False, None, {}, PermissionDecision.ALLOW),
        (False, "/etc/hosts", {}, PermissionDecision.ALLOW),
        (False, "/workspace/harness-eval-astra/app/.env", {}, PermissionDecision.ASK),
        (False, None, {"external_shell_enabled": False}, PermissionDecision.DENY),
        (False, None, {"home_shell_enabled": False}, PermissionDecision.DENY),
        (False, None, {"external_filesystem_policy": "deny"}, PermissionDecision.DENY),
        (False, None, {"home_filesystem_policy": "deny"}, PermissionDecision.DENY),
    ],
)
async def test_shell_policy_covers_incomplete_analysis(
    tmp_path, profile_factory, trust, classification, analysis_complete, path, agent_overrides, expected
) -> None:
    client = FakeExecutionClient(
        {}, {"shell.inspect", "shell.run"}, current_directory="/workspace/harness-eval-astra/app"
    )
    call = ToolCall("shell", "shell_run", {
        "command": 'for f in frontend/package.json frontend/vite.config.js; do cat "$f"; done'
    })
    context = _context(tmp_path, call, client)
    agent = ExecutionAgent(
        id=client.agent_id, user_id=context.session.user_id, home_directory="/workspace",
        home_filesystem_policy="auto", external_filesystem_policy="ask",
        home_shell_enabled=True, external_shell_enabled=True,
    )
    for key, value in agent_overrides.items():
        setattr(agent, key, value)
    context.db.scalar.return_value = agent
    context.db.scalars.return_value = Mock(all=Mock(return_value=[]))
    settings = ToolSettings().select_trust(trust)
    if profile_factory:
        profile = profile_factory()
        settings._agent_permissions = (
            profile.public_permissions if trust == TrustLevel.PUBLIC else profile.private_permissions
        )
    request = _shell_permission(
        client, classification=classification, prefixes=[["find"], ["find"], ["printf"], ["cat"]],
        paths=[path] if path else [], analysis_complete=analysis_complete,
    )
    policy = _tool_policy(
        settings, IntegrationSettings(), request,
        snapshot=permission_snapshot(settings, {"shell_run"}),
    )
    if profile_factory == plan_profile and classification == "write":
        assert policy == ToolExecutionPolicy.ASK
        if expected == PermissionDecision.ALLOW:
            expected = PermissionDecision.ASK
    elif profile_factory is None and (classification == "write" or trust == TrustLevel.PUBLIC):
        assert policy == ToolExecutionPolicy.ASK
        if expected == PermissionDecision.ALLOW:
            expected = PermissionDecision.ASK
    else:
        assert policy == ToolExecutionPolicy.AUTO
    assert await PermissionPolicy.evaluate(
        context.db, context.session, request, context.root_files, policy,
    ) == expected


@pytest.mark.parametrize(("command", "commands", "prefixes"), [
    (
        "cat README.md",
        [{"tokens": ["cat", "README.md"], "prefix": ["cat"], "classification": "read"}],
        [["cat"]],
    ),
    (
        "docker exec app cat /etc/hosts",
        [{"tokens": ["docker", "exec", "app", "cat", "/etc/hosts"],
          "prefix": None, "classification": "read"}],
        [],
    ),
    (
        "curl https://example.com; docker exec app cat /etc/hosts; cat README.md",
        [
            {"tokens": ["curl", "https://example.com"], "prefix": ["curl"], "classification": "read"},
            {"tokens": ["docker", "exec", "app", "cat", "/etc/hosts"],
             "prefix": None, "classification": "read"},
            {"tokens": ["cat", "README.md"], "prefix": ["cat"], "classification": "read"},
        ],
        [],
    ),
])
@pytest.mark.parametrize("session_temp_supported", [False, True])
async def test_shell_dispatches_with_inspection_classification(
    tmp_path, monkeypatch, command, commands, prefixes, session_temp_supported,
) -> None:
    call = ToolCall("shell", "shell_run", {"command": command})
    client = FakeExecutionClient(
        {
            "shell.inspect": ShellInspectResult(
                required_categories=["read"],
                targets=[],
                analysis_complete=True,
                classification="read",
                read_only=True,
                commands=commands,
                inspection_revision="a" * 64,
            ),
            "shell.run": ProcessResult(
                exit_code=0,
                timed_out=False,
                duration_ms=1,
                stdout_bytes=0,
                stderr_bytes=0,
                stdout_truncated=False,
                stderr_truncated=False,
            ),
        },
        {"shell.inspect", "shell.run"},
    )
    context = _context(tmp_path, call, client)
    context.tool_settings._temporary_files_enabled = True
    monkeypatch.setattr("mato_loop_backend.tools.access.profile_tool_settings", AsyncMock(return_value=context.tool_settings))
    monkeypatch.setattr("mato_loop_backend.tools.computer.execution_agent_connections.system_info", lambda _id: SystemInfo.model_validate({
        "temporaryDirectory": "/tmp/mato-loop",
        **({"sessionTemporaryDirectorySupported": True} if session_temp_supported else {}),
    }))
    context.db.get.return_value = ExecutionAgent(home_directory="/home/user", external_filesystem_policy="ask")

    result = await _tools(client.operations).execute(call, context)

    assert not result.is_error
    assert [operation for operation, _arguments in client.requests] == [
        "shell.inspect",
        "shell.run",
    ]
    assert client.requests[-1][1] == {
        "command": command,
        "currentDirectory": client.current_directory,
        "timeoutSeconds": None,
        "expectedInspectionRevision": "a" * 64,
        **({"temporarySessionId": str(context.session.id)} if session_temp_supported else {}),
    }
    request = context.ask_handler.await_args.args[0]
    assert request.capability == "shell.read"
    assert request.approval_details == {
        "shellClassification": "read",
        "shellCategories": ["read"],
        "shellAnalysisComplete": True,
        "shellPrefixes": prefixes,
    }
    assert json.loads(request.resource_scope)["prefixes"] == prefixes
    assert request.available_decisions == ("allow_once", "allow_session", "deny")
    allowed = [["docker"], ["curl"], ["cat"]]
    assert shell_prefix_allowed(ToolSettings(shell_prefixes={"read": allowed}), request) == bool(prefixes)
    granted = request
    scope = granted.effective_session_grants[0]
    grant = PermissionGrant(
        session_id=context.session.id,
        capability=scope.capability,
        resource_type=scope.resource_type,
        resource_scope=scope.resource_scope,
    )
    assert PermissionPolicy.grant_matches(grant, request, context.root_files)


@pytest.mark.parametrize("classification", ["read", "write"])
async def test_shell_resolves_variable_reference_only_for_process_transport(
    tmp_path, monkeypatch, classification
) -> None:
    reference = uuid4()
    tokens = ["env"] if classification == "read" else ["touch", "output"]
    call = ToolCall(
        "shell",
        "shell_run",
        {"command": " ".join(tokens), "environment": {"API_TOKEN": str(reference)}},
    )
    client = FakeExecutionClient(
        {
            "shell.inspect": ShellInspectResult(
                required_categories=[classification],
                targets=[],
                analysis_complete=True,
                classification=classification,
                read_only=classification == "read",
                commands=[
                    {
                        "tokens": tokens,
                        "prefix": [tokens[0]],
                        "classification": classification,
                    }
                ],
                inspection_revision="a" * 64,
            ),
            "shell.run": ProcessResult(
                exit_code=0,
                timed_out=False,
                duration_ms=1,
                stdout_bytes=0,
                stderr_bytes=0,
                stdout_truncated=False,
                stderr_truncated=False,
            ),
        },
        {"shell.inspect", "shell.run"},
    )
    request = client.request

    async def request_with_output(operation, arguments, result_model, **kwargs):
        if operation == "shell.run":
            await kwargs["on_event"](
                SimpleNamespace(event="stdout", data="token=host-secret\n")
            )
        return await request(operation, arguments, result_model, **kwargs)

    client.request = request_with_output
    context = _context(tmp_path, call, client)
    variable = Credential(
        id=reference,
        user_id=context.session.user_id,
        kind="personal_variable",
        label="TOKEN",
        encrypted_payload=b"encrypted",
        metadata_json={"secret": True},
    )
    from mato_loop_backend.tools import secrets as tool_secrets

    _mock_value = AsyncMock(return_value=variable)
    for _mock_target in (tool_secrets, mock_tools_interaction,):
        monkeypatch.setattr(_mock_target, 'get_personal_variable', _mock_value)
    monkeypatch.setattr(tool_secrets, 'personal_variable_value', lambda _variable: "host-secret")
    monkeypatch.setattr(
        tool_secrets.tools_access, "profile_tool_settings",
        AsyncMock(return_value=context.tool_settings),
    )

    result = await _tools(client.operations).execute(call, context)

    assert not result.is_error
    assert client.requests[-1][1]["environment"] == {"API_TOKEN": "host-secret"}
    assert "host-secret" not in result.content
    assert "token=[redacted]" in result.content
    assert str(reference) in result.content
    permission = context.ask_handler.await_args_list[0].args[0]
    assert permission.capability == f"shell.{classification}"
    assert permission.approval_details["shellAnalysisComplete"] is False
    assert permission.sensitive_access is (classification == "read")
    assert context.ask_handler.await_args_list[1].args[0].capability == "secrets.access"


async def test_secret_references_keep_only_selected_fields_for_session(
    tmp_path,
) -> None:
    client = FakeExecutionClient(
        {
            "shell.inspect": ShellInspectResult(
                required_categories=["read"],
                targets=[],
                analysis_complete=True,
                classification="read",
                read_only=True,
                commands=[
                    {
                        "tokens": ["env"],
                        "prefix": ["env"],
                        "classification": "read",
                    }
                ],
                inspection_revision="a" * 64,
            ),
            "shell.run": ProcessResult(
                exit_code=0,
                timed_out=False,
                duration_ms=1,
                stdout_bytes=0,
                stderr_bytes=0,
                stdout_truncated=False,
                stderr_truncated=False,
            ),
        },
        {"shell.inspect", "shell.run"},
    )
    context = _context(
        tmp_path,
        ToolCall(
            "secret",
            "secret_input",
            {
                "inputs": [
                    {
                        "name": "SSH_USERNAME",
                        "label": "SSH username",
                        "question": "Enter the username",
                        "description": "SSH username for backup.example",
                        "masked": False,
                    },
                    {
                        "name": "SSH_PASSWORD",
                        "label": "SSH password",
                        "question": "Enter the password",
                    },
                ]
            },
        ),
        client,
    )
    context.question_handler = AsyncMock(
        return_value=ProtectedQuestionResolution(
            {
                "SSH_USERNAME": ["  exact user  "],
                "SSH_PASSWORD": ["  exact secret  "],
            },
            frozenset({"SSH_USERNAME"}),
        )
    )

    secret_result = await _tools(client.operations).execute(context.tool_call, context)

    environment = json.loads(secret_result.content)["environment"]
    assert set(environment) == {"SSH_USERNAME", "SSH_PASSWORD"}
    assert environment["SSH_USERNAME"].startswith("session-secret:")
    assert environment["SSH_PASSWORD"].startswith("run-secret:")
    assert "exact user" not in secret_result.content
    assert "exact secret" not in secret_result.content
    assert session_secrets.prompt_entries(context.session.id)["SSH_USERNAME"] == {
        "valueRef": environment["SSH_USERNAME"],
        "description": "SSH username for backup.example",
    }
    assert list(context.one_time_secrets.values()) == ["  exact secret  "]
    shell_call = ToolCall(
        "shell", "shell_run", {"command": "env", "environment": environment}
    )
    context.tool_call = shell_call

    first_result = await _tools(client.operations).execute(shell_call, context)

    assert not first_result.is_error
    assert list(context.one_time_secrets.values()) == ["  exact secret  "]
    assert client.requests[-1][1]["environment"] == {
        "SSH_USERNAME": "  exact user  ",
        "SSH_PASSWORD": "  exact secret  ",
    }
    assert len(context.ask_handler.await_args_list) == 1

    password_call = ToolCall(
        "shell-again",
        "shell_run",
        {
            "command": "env",
            "environment": {"SSH_PASSWORD": environment["SSH_PASSWORD"]},
        },
    )
    context.tool_call = password_call
    second_result = await _tools(client.operations).execute(password_call, context)

    assert not second_result.is_error
    assert client.requests[-1][1]["environment"] == {
        "SSH_PASSWORD": "  exact secret  "
    }
    assert list(context.one_time_secrets.values()) == ["  exact secret  "]

    # A new model call in the same run may ask again after an instant message.
    context.question_handler = AsyncMock(return_value=ProtectedQuestionResolution(
        {"API_TOKEN": ["new token"]}, frozenset(),
    ))
    repeated = ToolCall("secret-again", "secret_input", {"inputs": [
        {"name": "SSH_PASSWORD", "label": "SSH password", "question": "Enter the password"},
        {"name": "API_TOKEN", "label": "API token", "question": "Enter the token"},
    ]})
    context.tool_call = repeated
    repeated_result = await _tools(client.operations).execute(repeated, context)
    repeated_environment = json.loads(repeated_result.content)["environment"]
    assert repeated_environment["SSH_PASSWORD"] == environment["SSH_PASSWORD"]
    assert [item.id for item in context.question_handler.await_args.args[0].questions] == ["API_TOKEN"]
    assert "new token" not in repeated_result.content
    assert json.loads((await _tools(client.operations).execute(repeated, context)).content)[
        "environment"
    ] == repeated_environment
    assert context.question_handler.await_count == 1

    other_context = _context(tmp_path, password_call, client)
    isolated_result = await _tools(client.operations).execute(
        password_call, other_context
    )

    assert isolated_result.is_error
    assert json.loads(isolated_result.content)["error"]["code"] == (
        "secret.valueRefUnavailable"
    )
    session_secrets.clear_session(context.session.id)


@pytest.mark.parametrize("classification", ["read", "write"])
@pytest.mark.parametrize("analysis_complete", [False, True])
async def test_shell_copy_separates_classification_and_completeness(
    tmp_path, classification, analysis_complete,
) -> None:
    call = ToolCall("shell", "shell_run", {"command": "unknown-command --inspect"})
    client = FakeExecutionClient(
        {
            "shell.inspect": ShellInspectResult(
                required_categories=[classification],
                targets=[],
                analysis_complete=analysis_complete,
                classification=classification,
                read_only=classification == "read",
                commands=[],
                inspection_revision="b" * 64,
            ),
            "shell.run": ProcessResult(
                exit_code=0,
                timed_out=False,
                duration_ms=1,
                stdout_bytes=0,
                stderr_bytes=0,
                stdout_truncated=False,
                stderr_truncated=False,
            ),
        },
        {"shell.inspect", "shell.run"},
    )
    context = _context(tmp_path, call, client)

    result = await _tools(client.operations).execute(call, context)

    assert not result.is_error
    request = context.ask_handler.await_args.args[0]
    assert request.capability == f"shell.{classification}"
    assert request.risk_level == ("low" if classification == "read" else "high")
    assert request.title == (
        "Shell: Lesen erlauben?"
        if classification == "read"
        else "Shell: Lesen und Schreiben erlauben?"
    )
    assert request.summary == (
        "Es wurden keine bekannten Schreibaktionen erkannt. Der Befehl kann dennoch Nebenwirkungen haben."
        if classification == "read"
        else "Der Befehl wurde als schreibend eingestuft und kann Dateien verändern, Programme starten und Netzwerkdienste verwenden."
    ) + (
        " Die Analyse ist unvollständig; nicht alle Befehle und Pfade konnten geprüft werden."
        if not analysis_complete else ""
    )
    assert request.approval_details == {
        "shellClassification": classification,
        "shellCategories": [classification],
        "shellAnalysisComplete": analysis_complete,
        "shellPrefixes": [],
    }


@pytest.mark.parametrize("timeout", [0, -1, 3600.1, float("inf"), float("nan"), True, "60"])
def test_shell_timeout_rejects_invalid_values(timeout) -> None:
    with pytest.raises(ValidationError):
        ShellRunArguments(command="pwd", timeout=timeout)


async def test_shell_preparation_uses_the_execution_inspection_without_running_a_command(tmp_path):
    from mato_loop_backend.tools.permissions import authorize_session_requests, builtin_permissions

    call = ToolCall("prepare", "shell_run", {"command": "pwd"})
    client = FakeExecutionClient({"shell.inspect": ShellInspectResult(
        required_categories=["read"],
        targets=[], commands=[], read_only=True, classification="read", analysis_complete=True,
        inspection_revision="a" * 64,
    )}, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, call, client)
    context.ask_handler.side_effect = [PermissionDecision.ASK, PermissionDecision.ALLOW]
    requests = await builtin_permissions(ShellRunArguments(command="pwd"), context, "shell_run")
    result = await authorize_session_requests(context, requests)
    assert (result.is_error, client.requests, context.ask_handler.await_args.args[0].available_decisions) == (
        False, [("shell.inspect", {"command": "pwd", "currentDirectory": client.current_directory})],
        ("allow_session", "deny"),
    )


async def test_managed_delete_preparation_keeps_the_delete_category(tmp_path):
    from mato_loop_backend.tools.files import DeletePathArguments
    from mato_loop_backend.tools.permissions import builtin_permissions

    call = ToolCall("prepare", "delete_path", {"root": "session", "path": "output"})
    context = _context(tmp_path, call, FakeExecutionClient({}, set()))
    requests = await builtin_permissions(DeletePathArguments(**call.arguments), context, "delete_path")
    assert requests[0].effective_session_grants[0].capability == "category.delete"


@pytest.mark.parametrize("timeout", [None, 0.25, 3600])
@pytest.mark.parametrize(
    ("exit_code", "timed_out", "error_code"),
    [(0, False, None), (7, False, "process.commandFailed"), (-15, True, "process.timedOut"),
     (0, True, "process.timedOut")],
)
async def test_shell_timeout_and_process_evidence(
    tmp_path, timeout, exit_code, timed_out, error_code,
) -> None:
    call = ToolCall("shell", "shell_run", {"command": "printf evidence", "timeout": timeout})
    client = FakeExecutionClient({}, {"shell.inspect", "shell.run", "terminal.run"})
    process = ProcessResult(
        exit_code=exit_code, timed_out=timed_out, duration_ms=123, stdout_bytes=8,
        stderr_bytes=10, stdout_truncated=True, stderr_truncated=False,
    )
    inspection = ShellInspectResult(
        required_categories=["read"],
        targets=[], analysis_complete=True, classification="read", read_only=True,
        commands=[{"tokens": ["printf", "evidence"], "prefix": ["printf"], "classification": "read"}],
        inspection_revision="a" * 64,
    )

    async def execute(*_args, **kwargs):
        await kwargs["on_event"](Mock(event="stdout", data="evidence"))
        await kwargs["on_event"](Mock(event="stderr", data="diagnostic"))
        return process

    async def request(operation, arguments, _model, **kwargs):
        if operation == "shell.inspect":
            assert arguments == {"command": "printf evidence", "currentDirectory": client.current_directory}
            return inspection
        assert arguments["timeoutSeconds"] == timeout
        assert arguments["expectedInspectionRevision"] == "a" * 64
        assert kwargs["timeout"] == 3610
        return await execute(**kwargs)

    client.request = AsyncMock(side_effect=request)
    context = _context(tmp_path, call, client)
    result = await _tools(client.operations).execute(call, context)
    payload = json.loads(result.content)

    assert result.is_error == (error_code is not None)
    assert result.metadata.get("errorCode") == error_code
    assert payload["exitCode"] == exit_code
    assert payload["timedOut"] == timed_out
    assert payload["durationMs"] == 123
    assert payload["stdoutBytes"] == 8
    assert payload["stderrBytes"] == 10
    assert payload["output"] == {"stdout": "evidence", "stderr": "diagnostic", "truncated": True}
    assert context.ask_handler.await_count == 1
    assert client.request.await_count == 2


@pytest.mark.parametrize("deny", [False, True])
async def test_shell_timeout_does_not_bypass_permission_or_inspection_revision(tmp_path, deny) -> None:
    call = ToolCall("shell", "shell_run", {"command": "cat README.md", "timeout": 3600})

    def changed(arguments):
        assert arguments["expectedInspectionRevision"] == "a" * 64
        assert arguments["timeoutSeconds"] == 3600
        raise ExecutionClientError("process.inspectionChanged")

    client = FakeExecutionClient(
        {
            "shell.inspect": ShellInspectResult(
                required_categories=["read"],
                targets=[], analysis_complete=True, classification="read", read_only=True,
                commands=[{"tokens": ["cat", "README.md"], "prefix": ["cat"], "classification": "read"}],
                inspection_revision="a" * 64,
            ),
            "shell.run": changed,
        },
        {"shell.inspect", "shell.run"},
    )
    context = _context(
        tmp_path, call, client, decision=PermissionDecision.DENY if deny else PermissionDecision.ALLOW,
    )
    result = await _tools(client.operations).execute(call, context)
    assert result.is_error
    assert result.metadata["errorCode"] == ("permission.denied" if deny else "process.inspectionChanged")
    assert len(client.requests) == (1 if deny else 2)
    assert context.ask_handler.await_count == 1


@pytest.mark.parametrize("profile_factory", [plan_profile, build_profile, None])
@pytest.mark.parametrize("trust", [TrustLevel.PUBLIC, TrustLevel.PRIVATE])
@pytest.mark.parametrize("classification", ["read", "write"])
@pytest.mark.parametrize("analysis_complete", [False, True])
async def test_shell_metadata_only_paths_preserve_profile_and_trust_policies(
    tmp_path, profile_factory, trust, classification, analysis_complete,
) -> None:
    command = "command -v python3; ls /usr/bin /usr/local/bin 2>/dev/null"
    if classification == "write":
        command += "; touch result.txt"
    call = ToolCall("shell", "shell_run", {"command": command})
    targets = [
        {"path": "/usr/bin", "kind": "toolchain_metadata"},
        {"path": "/usr/local/bin", "kind": "toolchain_metadata"},
        {"path": "/usr/bin/python3", "kind": "toolchain_metadata"},
        {"path": "/dev/null", "kind": "discard"},
        {"path": "/home/user/project/result.txt", "kind": "path", "access": classification},
    ]
    inspection = ShellInspectResult.model_validate({
        "requiredCategories": [classification],
        "targets": targets, "analysisComplete": analysis_complete,
        "classification": classification, "readOnly": classification == "read",
        "commands": [
            {"tokens": ["command", "-v", "python3"], "prefix": ["command"], "classification": "read"},
            {"tokens": ["ls", "/usr/bin", "/usr/local/bin"], "prefix": ["ls"], "classification": "read"},
            *([{"tokens": ["touch", "result.txt"], "prefix": ["touch"], "classification": "write"}]
              if classification == "write" else []),
        ],
        "inspectionRevision": "a" * 64,
    })
    client = FakeExecutionClient({
        "shell.inspect": inspection,
        "shell.run": ProcessResult(
            exit_code=0, timed_out=False, duration_ms=1, stdout_bytes=0, stderr_bytes=0,
            stdout_truncated=False, stderr_truncated=False,
        ),
    }, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, call, client)
    context.db.scalar.return_value = ExecutionAgent(
        id=client.agent_id, user_id=context.session.user_id, home_directory="/home/user",
        home_filesystem_policy="auto", external_filesystem_policy="ask",
        home_shell_enabled=True, external_shell_enabled=True,
    )
    context.db.scalars.return_value = Mock(all=Mock(return_value=[]))
    settings = ToolSettings().select_trust(trust)
    profile = profile_factory() if profile_factory else None
    if profile:
        settings._agent_permissions = (
            profile.public_permissions if trust == TrustLevel.PUBLIC else profile.private_permissions
        )
    expected_policy = (
        ToolExecutionPolicy.ASK
        if profile_factory == plan_profile and classification == "write"
        else ToolExecutionPolicy.ASK
        if profile is None and (classification == "write" or trust == TrustLevel.PUBLIC)
        else ToolExecutionPolicy.AUTO
    )

    async def authorize(request):
        policy = _tool_policy(
            settings, IntegrationSettings(), request,
            snapshot=permission_snapshot(settings, {"shell_run"}),
        )
        assert policy == expected_policy
        return await PermissionPolicy.evaluate(
            context.db, context.session, request, context.root_files, policy,
            agent_profile_id=profile.id if profile else None, trust_level=trust,
        )

    context.ask_handler = AsyncMock(side_effect=authorize)
    result = await _tools(client.operations).execute(call, context)
    assert result.is_error == (expected_policy != ToolExecutionPolicy.AUTO)
    request = context.ask_handler.await_args.args[0]
    assert request.shell_metadata_paths == frozenset(target["path"] for target in targets[:-1])
    assert json.loads(request.resource_scope) == {
        "requiredCategories": [classification],
        "agentId": str(client.agent_id), "currentDirectory": client.current_directory,
        "action": shell_action_scope(client.agent_id, client.current_directory, command),
        "classification": classification, "analysisComplete": analysis_complete,
        "prefixes": [unit.prefix for unit in inspection.commands],
        "paths": [target["path"] for target in targets],
        "targetAccesses": ["exempt", "exempt", "exempt", "exempt", classification],
    }
    assert set(request.approval_details) == {
        "shellClassification", "shellCategories", "shellAnalysisComplete", "shellPrefixes",
    }
    assert [operation for operation, _ in client.requests] == (
        ["shell.inspect", "shell.run"] if not result.is_error else ["shell.inspect"]
    )
    if not result.is_error:
        assert client.requests[-1][1]["expectedInspectionRevision"] == "a" * 64


@pytest.mark.parametrize("classification", ["read", "write"])
@pytest.mark.parametrize("kind", ["path", "input", "output", "executable", "script", "source", "directory"])
@pytest.mark.parametrize("conflicting_path", ["/usr/bin/python3", "/dev/null"])
async def test_shell_ordinary_target_wins_over_metadata_for_the_same_path(
    tmp_path, classification, kind, conflicting_path,
) -> None:
    call = ToolCall("shell", "shell_run", {"command": "command -v python3; cat /usr/bin/python3"})
    target_access = (
        "write" if kind == "output"
        else "read" if kind in {"input", "executable", "script", "source"}
        else classification
    )
    effective_classification = "write" if target_access == "write" else classification
    client = FakeExecutionClient({
        "shell.inspect": ShellInspectResult(
            required_categories=[effective_classification],
            targets=[
                {"path": "/usr/bin/python3", "kind": "toolchain_metadata"},
                {"path": "/dev/null", "kind": "discard"},
                {"path": conflicting_path, "kind": kind, "access": target_access},
            ],
            analysis_complete=False, classification=effective_classification,
            read_only=effective_classification == "read",
            commands=[], inspection_revision="a" * 64,
        ),
    }, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, call, client, decision=PermissionDecision.ASK)
    result = await _tools(client.operations).execute(call, context)
    request = context.ask_handler.await_args.args[0]
    assert result.is_error
    assert request.shell_metadata_paths == frozenset({"/usr/bin/python3", "/dev/null"}) - {conflicting_path}
    assert json.loads(request.resource_scope)["paths"] == [
        "/usr/bin/python3", "/dev/null", conflicting_path,
    ]
    context.db.scalar.return_value = ExecutionAgent(
        id=client.agent_id, user_id=context.session.user_id, home_directory="/home/user",
        home_filesystem_policy="auto", external_filesystem_policy="ask",
        home_shell_enabled=True, external_shell_enabled=True,
    )
    context.db.scalars.return_value = Mock(all=Mock(return_value=[]))
    assert await PermissionPolicy.evaluate(
        context.db, context.session, request, context.root_files, ToolExecutionPolicy.AUTO,
    ) == PermissionDecision.ALLOW


@pytest.mark.parametrize("classification", ["read", "write"])
@pytest.mark.parametrize("analysis_complete", [False, True])
@pytest.mark.parametrize(("path", "overrides", "expected"), [
    ("/usr/bin", {}, PermissionDecision.ALLOW),
    ("/dev/null", {}, PermissionDecision.ALLOW),
    ("/usr/bin", {"external_shell_enabled": False}, PermissionDecision.DENY),
    ("/dev/null", {"external_shell_enabled": False}, PermissionDecision.DENY),
    ("/usr/bin", {"external_filesystem_policy": "deny"}, PermissionDecision.DENY),
    ("/dev/null", {"external_filesystem_policy": "deny"}, PermissionDecision.DENY),
    ("/usr/bin", {"home_shell_enabled": False}, PermissionDecision.DENY),
    ("/usr/bin", {"home_filesystem_policy": "deny"}, PermissionDecision.DENY),
    ("/usr/bin/.env", {}, PermissionDecision.ASK),
    ("/usr/bin", {"unavailable": True}, PermissionDecision.DENY),
])
async def test_shell_metadata_paths_do_not_bypass_hard_checks(
    tmp_path, classification, analysis_complete, path, overrides, expected,
) -> None:
    client = FakeExecutionClient({}, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, ToolCall("shell", "shell_run", {}), client)
    agent = ExecutionAgent(
        id=client.agent_id, user_id=context.session.user_id, home_directory="/home/user",
        home_filesystem_policy="auto", external_filesystem_policy="ask",
        home_shell_enabled=True, external_shell_enabled=True,
    )
    for key, value in overrides.items():
        if key != "unavailable":
            setattr(agent, key, value)
    context.db.scalar.return_value = None if overrides.get("unavailable") else agent
    context.db.scalars.return_value = Mock(all=Mock(return_value=[]))
    request = replace(
        _shell_permission(client, classification=classification, paths=[path], analysis_complete=analysis_complete),
        shell_metadata_paths=frozenset({path}),
    )
    assert await PermissionPolicy.evaluate(
        context.db, context.session, request, context.root_files, ToolExecutionPolicy.AUTO,
    ) == expected
    statement = context.db.scalar.await_args.args[0]
    assert statement.get_execution_options()["populate_existing"] is True
    assert "revoked_at IS NULL" in str(statement)
    assert context.session.user_id in statement.compile().params.values()
    assert client.agent_id in statement.compile().params.values()


async def test_shell_metadata_must_be_current_typed_scope_subset(tmp_path) -> None:
    client = FakeExecutionClient({}, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, ToolCall("shell", "shell_run", {}), client)
    context.db.scalar.return_value = ExecutionAgent(
        id=client.agent_id, user_id=context.session.user_id, home_directory="/home/user",
        home_filesystem_policy="auto", external_filesystem_policy="ask",
        home_shell_enabled=True, external_shell_enabled=True,
    )
    context.db.scalars.return_value = Mock(all=Mock(return_value=[]))
    request = _shell_permission(client, classification="read", paths=["/etc/hosts"])
    assert request.shell_metadata_paths == frozenset()
    for details in ({"shell_metadata_paths": ["/etc/hosts"]}, {"shellMetadataPaths": ["/etc/hosts"]}):
        assert await PermissionPolicy.evaluate(
            context.db, context.session, replace(request, approval_details=details),
            context.root_files, ToolExecutionPolicy.AUTO,
        ) == PermissionDecision.ALLOW
        with pytest.raises(ValidationError):
            ShellRunArguments(command="cat /etc/hosts", **details)
    assert await PermissionPolicy.evaluate(
        context.db, context.session,
        replace(request, shell_metadata_paths=frozenset({"/usr/bin"})),
        context.root_files, ToolExecutionPolicy.AUTO,
    ) == PermissionDecision.DENY
    # Typed filesystem reads never use the shell-only exemption.
    direct_read = execution_directory_permission(client.agent_id, "/usr/bin", capability="filesystem.read")
    assert await PermissionPolicy.evaluate(
        context.db, context.session,
        replace(direct_read, shell_metadata_paths=frozenset({"/usr/bin"})),
        context.root_files, ToolExecutionPolicy.AUTO,
    ) == PermissionDecision.ASK


async def test_shell_reinspection_keeps_existing_grants_class_bound(tmp_path) -> None:
    call = ToolCall("shell", "shell_run", {"command": "command -v python3"})
    inspection = ShellInspectResult(
        required_categories=["read"],
        targets=[{"path": "/usr/bin/python3", "kind": "toolchain_metadata"}],
        analysis_complete=False, classification="read", read_only=True,
        commands=[{"tokens": ["command", "-v", "python3"], "prefix": ["command"], "classification": "read"}],
        inspection_revision="a" * 64,
    )
    client = FakeExecutionClient({"shell.inspect": inspection}, {"shell.inspect", "shell.run"})
    context = _context(tmp_path, call, client)
    context.db.scalar.return_value = ExecutionAgent(
        id=client.agent_id, user_id=context.session.user_id, home_directory="/home/user",
        home_filesystem_policy="auto", external_filesystem_policy="ask",
        home_shell_enabled=True, external_shell_enabled=True,
    )
    grants = []
    context.db.scalars.return_value = Mock(all=Mock(side_effect=lambda: grants))

    async def authorize(request):
        return await PermissionPolicy.evaluate(
            context.db, context.session, request, context.root_files, ToolExecutionPolicy.ASK,
        )

    context.ask_handler = AsyncMock(side_effect=authorize)
    assert (await _tools(client.operations).execute(call, context)).is_error
    pending = context.ask_handler.await_args.args[0]
    scope = pending.effective_session_grants[0]
    grants.append(PermissionGrant(
        session_id=context.session.id, capability=scope.capability,
        resource_type=scope.resource_type, resource_scope=scope.resource_scope,
    ))
    client.responses["shell.inspect"] = ShellInspectResult.model_validate({
        **inspection.model_dump(), "classification": "write", "read_only": False,
        "required_categories": ["write"],
        "inspection_revision": "b" * 64,
    })
    assert (await _tools(client.operations).execute(call, context)).is_error
    reinspected = context.ask_handler.await_args.args[0]
    assert reinspected.capability == "shell.write"
    assert not PermissionPolicy.grant_matches(grants[0], reinspected, context.root_files)
    assert [operation for operation, _ in client.requests] == ["shell.inspect", "shell.inspect"]
    assert await authorize(pending) == PermissionDecision.ALLOW
    assert await PermissionPolicy.evaluate(
        context.db, context.session, pending, context.root_files, ToolExecutionPolicy.DISABLED,
    ) == PermissionDecision.DENY
