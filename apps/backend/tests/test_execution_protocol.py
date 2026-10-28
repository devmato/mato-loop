import asyncio
import base64
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from mato_loop_backend.execution.client import (
    ExecutionClient,
    ExecutionClientError,
    ProcessResult,
    ShellCommand,
    ShellInspectResult,
    SystemInfoResult,
    TerminalOpenResult,
    TerminalReadResult,
    WorkspaceWriteResult,
)
from mato_loop_backend.execution.manager import (
    ExecutionAgentConnectionManager,
    ExecutionAgentError,
    disconnect_error_code,
)
from mato_loop_backend.execution.protocol import (
    SUPPORTED_OPERATIONS,
    HelloMessage,
    ResponseMessage,
    SystemInfo,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: list[tuple[int, str]] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self, *, code: int, reason: str) -> None:
        self.closed.append((code, reason))


def _hello(agent_id=None, *, operations=None) -> HelloMessage:
    return HelloMessage.model_validate(
        {
            "type": "hello",
            "protocolVersion": 1,
            "agentId": agent_id or uuid4(),
            "agentVersion": "1.0.0",
            "computerName": "test-host",
            "platform": "linux",
            "operations": operations
            or ["filesystem.list", "filesystem.read", "filesystem.write"],
            "homeDirectory": "/home/test",
        }
    )


def test_protocol_is_strict_and_requires_unique_operations() -> None:
    payload = _hello().model_dump(by_alias=True)
    payload["extra"] = True
    with pytest.raises(ValidationError):
        HelloMessage.model_validate(payload)

    payload.pop("extra")
    payload["operations"] *= 2
    with pytest.raises(ValidationError):
        HelloMessage.model_validate(payload)

    payload = _hello().model_dump(by_alias=True)
    payload["protocolVersion"] = 3
    with pytest.raises(ValidationError):
        HelloMessage.model_validate(payload)


def test_hello_home_directory_requires_a_normalized_absolute_posix_path() -> None:
    payload = _hello().model_dump(by_alias=True)
    payload["homeDirectory"] = "/"
    assert HelloMessage.model_validate(payload).home_directory == "/"

    for path in (
        "home/test",
        "/home\\test",
        "/home//test",
        "/home/./test",
        "/home/../test",
        "/home/\x1ftest",
        "/home/\x80test",
    ):
        payload["homeDirectory"] = path
        with pytest.raises(ValidationError):
            HelloMessage.model_validate(payload)


def test_hello_system_info_is_optional_bounded_and_strict() -> None:
    payload = _hello().model_dump(by_alias=True)
    payload.pop("systemInfo")
    assert HelloMessage.model_validate(payload).system_info is None
    info = {
        "operatingSystem": "Linux", "distribution": "Debian GNU/Linux 13 (trixie)",
        "kernel": "6.12", "architecture": "x86_64", "cpuModel": "AMD Ryzen 9 7950X",
        "logicalCpuCount": 32, "totalMemoryBytes": 64 * 1024**3,
        "temporaryDirectory": "/tmp/mato-loop",
        "sessionTemporaryDirectorySupported": True,
    }
    payload["systemInfo"] = info
    assert HelloMessage.model_validate(payload).system_info.model_dump(by_alias=True) == info
    live = SystemInfoResult.model_validate({
        "agentVersion": "1.0.0", "platform": "linux", **info,
    })
    assert live.cpu_model == info["cpuModel"]
    assert live.total_memory_bytes == info["totalMemoryBytes"]
    unknown = SystemInfoResult.model_validate({"agentVersion": "1.0.0", "platform": "linux"})
    assert unknown.operating_system is None
    assert unknown.logical_cpu_count is None
    for invalid in (
        {"logicalCpuCount": True}, {"logicalCpuCount": "8"}, {"logicalCpuCount": 0},
        {"logicalCpuCount": 2147483648}, {"totalMemoryBytes": -1},
        {"totalMemoryBytes": 9223372036854775808}, {"cpuModel": "x" * 256},
        {"operatingSystem": ""}, {"availableMemoryBytes": 1024},
    ):
        payload["systemInfo"] = invalid
        with pytest.raises(ValidationError):
            HelloMessage.model_validate(payload)


async def test_system_info_follows_connection_lifetime_and_agent_identity() -> None:
    manager = ExecutionAgentConnectionManager()
    hello = _hello()
    old, new, other = FakeWebSocket(), FakeWebSocket(), FakeWebSocket()
    hello.system_info = SystemInfo(cpu_model="Old CPU", total_memory_bytes=1024)
    await manager.register(hello, old)
    other_hello = _hello()
    other_hello.system_info = SystemInfo(cpu_model="Other CPU")
    await manager.register(other_hello, other)
    assert manager.system_info(hello.agent_id).cpu_model == "Old CPU"
    assert manager.system_info(uuid4()) is None

    hello.system_info = SystemInfo(cpu_model="New CPU", total_memory_bytes=2048)
    await manager.register(hello, new)
    await manager.unregister(hello.agent_id, old)
    assert manager.system_info(hello.agent_id).total_memory_bytes == 2048
    assert manager.system_info(other_hello.agent_id).cpu_model == "Other CPU"
    await manager.unregister(hello.agent_id, new)
    assert manager.system_info(hello.agent_id) is None

    await manager.register(hello, new)
    hello.system_info = None
    await manager.register(hello, old)
    assert manager.system_info(hello.agent_id) is None
    await manager.close(other_hello.agent_id)
    assert manager.system_info(other_hello.agent_id) is None


def test_supported_operations_match_the_execution_agent() -> None:
    assert SUPPORTED_OPERATIONS == {
        "git.changes",
        "git.repository",
        "git.worktree",
        "git.worktree.remove",
        "filesystem.stat",
        "filesystem.glob",
        "filesystem.grep",
        "filesystem.list",
        "filesystem.read",
        "filesystem.write",
        "filesystem.mkdir",
        "filesystem.rename",
        "filesystem.delete",
        "filesystem.edit",
        "filesystem.copy",
        "filesystem.move",
        "filesystem.patch",
        "filesystem.import",
        "filesystem.export",
        "filesystem.upsert",
        "shell.run",
        "administrator.run",
        "cache.ensure",
        "cache.skills.prepare",
        "cache.skills.release",
        "cache.export",
        "cache.store",
        "cache.clear",
        "system.info",
        "filesystem.resolve",
        "integration.openscad.discover",
        "integration.openscad.render",
        "integration.openscad.render6",
        "integration.openscad.test",
        "integration.browser.status",
        "integration.browser.setup",
        "integration.browser.setupread",
        "integration.browser.setupwrite",
        "integration.browser.setupresize",
        "integration.browser.open",
        "integration.browser.control",
        "integration.browser.resize",
        "integration.browser.navigation",
        "integration.browser.clipboard",
        "integration.browser.view",
        "integration.browser.close",
        "integration.browser.execute",
        "integration.browser.file",
        "integration.browser.files",
        "integration.browser.upload",
        "maintenance.start",
        "maintenance.update",
        "maintenance.update.interrupt",
        "shell.inspect",
        "shell.classify",
        "terminal.open",
        "terminal.read",
        "terminal.write",
        "terminal.resize",
        "terminal.close",
        "terminal.run",
    }


def test_terminal_results_use_typed_wire_fields() -> None:
    terminal_id = uuid4()
    opened = TerminalOpenResult.model_validate({"terminalId": str(terminal_id)})
    read = TerminalReadResult.model_validate(
        {"data": "aGVsbG8=", "eof": True, "exitCode": 0}
    )

    assert opened.terminal_id == terminal_id
    with pytest.raises(ValidationError):
        TerminalOpenResult.model_validate({"terminalId": "personal-1"})
    assert read.data == "aGVsbG8="
    assert read.eof is True
    assert read.exit_code == 0
    with pytest.raises(ValidationError):
        TerminalReadResult.model_validate({"data": "not base64", "eof": False})
    with pytest.raises(ValidationError):
        TerminalReadResult.model_validate(
            {"data": base64.b64encode(b"a" * 65_537).decode(), "eof": False}
        )


async def test_manager_correlates_events_and_response_and_ignores_unknown_operations() -> None:
    manager = ExecutionAgentConnectionManager()
    websocket = FakeWebSocket()
    hello = _hello(operations=["filesystem.read", "future.unknown"])
    await manager.register(hello, websocket)  # type: ignore[arg-type]
    events = []

    task = asyncio.create_task(
        manager.request(
            hello.agent_id,
            "filesystem.read",
            {"path": "/home/test/README.md"},
            on_event=events.append,
        )
    )
    await asyncio.sleep(0)
    request = json.loads(websocket.sent[0])
    for _ in range(1_025):
        await manager.receive(
            hello.agent_id,
            websocket,  # type: ignore[arg-type]
            json.dumps(
                {"type": "event", "id": request["id"], "event": "stdout", "data": "one"}
            ),
        )
    await manager.receive(
        hello.agent_id,
        websocket,  # type: ignore[arg-type]
        json.dumps({"type": "response", "id": request["id"], "ok": True, "result": {"x": 1}}),
    )

    response = await task
    assert response.result == {"x": 1}
    assert [event.data for event in events] == ["one"] * 1_025
    with pytest.raises(ExecutionAgentError) as exc_info:
        await manager.request(hello.agent_id, "future.unknown", {})
    assert exc_info.value.code == "executionAgent.operationUnavailable"


@pytest.mark.parametrize("data", ["", "output"])
async def test_manager_bounds_output_including_empty_events(monkeypatch, data) -> None:
    monkeypatch.setattr("mato_loop_backend.execution.manager.MAX_OUTPUT_BYTES", 32)
    manager = ExecutionAgentConnectionManager()
    websocket = FakeWebSocket()
    hello = _hello(operations=["shell.run"])
    await manager.register(hello, websocket)
    task = asyncio.create_task(manager.request(hello.agent_id, "shell.run", {}))
    await asyncio.sleep(0)
    request_id = json.loads(websocket.sent[0])["id"]
    event = json.dumps({"type": "event", "id": request_id, "event": "stdout", "data": data})
    try:
        for _ in range(32 // max(1, len(data))):
            await manager.receive(hello.agent_id, websocket, event)
        with pytest.raises(ExecutionAgentError, match="process.outputTooLarge"):
            await manager.receive(hello.agent_id, websocket, event)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_latest_connection_replaces_old_and_disconnects_are_classified() -> None:
    manager = ExecutionAgentConnectionManager()
    agent_id = uuid4()
    old = FakeWebSocket()
    new = FakeWebSocket()
    hello = _hello(agent_id)
    await manager.register(hello, old)  # type: ignore[arg-type]
    task = asyncio.create_task(manager.request(agent_id, "filesystem.read", {}))
    await asyncio.sleep(0)

    await manager.register(hello, new)  # type: ignore[arg-type]

    with pytest.raises(ExecutionAgentError) as exc_info:
        await task
    assert exc_info.value.code == "executionAgent.replaced"
    assert old.closed == [(4001, "Replaced by a newer connection")]
    assert manager.snapshot(agent_id) is not None
    assert disconnect_error_code("filesystem.read", True) == "executionAgent.unavailable"
    assert disconnect_error_code("filesystem.write", True) == "executionAgent.outcomeUnknown"
    assert disconnect_error_code("git.worktree", True) == "executionAgent.outcomeUnknown"
    assert disconnect_error_code("filesystem.upsert", True) == "executionAgent.outcomeUnknown"
    assert disconnect_error_code("filesystem.edit", True) == "executionAgent.outcomeUnknown"
    assert disconnect_error_code("filesystem.patch", True) == "executionAgent.outcomeUnknown"
    assert disconnect_error_code("filesystem.rename", True) == "executionAgent.outcomeUnknown"
    assert disconnect_error_code("shell.run", True) == "process.outcomeUnknown"


async def test_generation_pinned_client_never_dispatches_to_replacement(monkeypatch) -> None:
    from mato_loop_backend.execution import client as client_module

    manager = ExecutionAgentConnectionManager()
    hello = _hello(operations=["shell.run"])
    first, second = FakeWebSocket(), FakeWebSocket()
    await manager.register(hello, first)
    client = ExecutionClient(hello.agent_id, frozenset(hello.operations), expected_generation=manager.generation(hello.agent_id))
    monkeypatch.setattr(client_module, "execution_agent_connections", manager)
    await manager.register(hello, second)
    with pytest.raises(ExecutionClientError, match="executionAgent.replaced"):
        await client.request("shell.run", {}, ProcessResult)
    assert not first.sent and not second.sent


async def test_replacement_cannot_miss_request_waiting_to_send() -> None:
    manager = ExecutionAgentConnectionManager()
    agent_id = uuid4()
    old = FakeWebSocket()
    new = FakeWebSocket()
    hello = _hello(agent_id)
    await manager.register(hello, old)  # type: ignore[arg-type]
    connection = manager._connections[agent_id]
    await connection.send_lock.acquire()
    task = asyncio.create_task(manager.request(agent_id, "filesystem.read", {}))
    await asyncio.sleep(0)

    await manager.register(hello, new)  # type: ignore[arg-type]
    connection.send_lock.release()

    with pytest.raises(ExecutionAgentError) as exc_info:
        await task
    assert exc_info.value.code == "executionAgent.replaced"
    assert old.sent == []


async def test_caller_cancellation_sends_cancel_and_ignores_late_response() -> None:
    manager = ExecutionAgentConnectionManager()
    websocket = FakeWebSocket()
    hello = _hello()
    await manager.register(hello, websocket)  # type: ignore[arg-type]
    task = asyncio.create_task(manager.request(hello.agent_id, "filesystem.read", {}))
    await asyncio.sleep(0)
    request_id = json.loads(websocket.sent[0])["id"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert json.loads(websocket.sent[1]) == {"type": "cancel", "id": request_id}
    await manager.receive(
        hello.agent_id,
        websocket,  # type: ignore[arg-type]
        json.dumps(
            {"type": "response", "id": request_id, "ok": True, "result": {}}
        ),
    )
    assert manager.is_connected(hello.agent_id)


async def test_caller_cancellation_abandons_dispatched_workspace_mutation() -> None:
    manager = ExecutionAgentConnectionManager()
    websocket = FakeWebSocket()
    hello = _hello(operations=["filesystem.write"])
    await manager.register(hello, websocket)  # type: ignore[arg-type]
    task = asyncio.create_task(
        manager.request(hello.agent_id, "filesystem.write", {})
    )
    await asyncio.sleep(0)
    request_id = json.loads(websocket.sent[0])["id"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [json.loads(item)["type"] for item in websocket.sent] == ["request", "cancel"]

    await manager.receive(
        hello.agent_id,
        websocket,  # type: ignore[arg-type]
        json.dumps(
            {
                "type": "response",
                "id": request_id,
                "ok": True,
                "result": {"path": "a.txt"},
            }
        ),
    )
    assert manager.request_ids(hello.agent_id) == ()


async def test_default_timeout_is_bounded_and_cancels_request() -> None:
    manager = ExecutionAgentConnectionManager(default_timeout=0.01)
    websocket = FakeWebSocket()
    hello = _hello()
    await manager.register(hello, websocket)  # type: ignore[arg-type]

    with pytest.raises(ExecutionAgentError) as exc_info:
            await manager.request(hello.agent_id, "filesystem.read", {})

    assert exc_info.value.code == "executionAgent.timeout"
    assert [json.loads(item)["type"] for item in websocket.sent] == ["request", "cancel"]
    assert manager.request_ids(hello.agent_id) == ()


async def test_dispatched_mutation_and_shell_timeouts_have_unknown_outcomes() -> None:
    for operation, expected_code in (
        ("filesystem.write", "executionAgent.outcomeUnknown"),
        ("git.worktree", "executionAgent.outcomeUnknown"),
        ("shell.run", "process.outcomeUnknown"),
    ):
        manager = ExecutionAgentConnectionManager(default_timeout=0.01)
        websocket = FakeWebSocket()
        hello = _hello(operations=[operation])
        await manager.register(hello, websocket)  # type: ignore[arg-type]

        with pytest.raises(ExecutionAgentError) as exc_info:
            await manager.request(hello.agent_id, operation, {})

        assert exc_info.value.code == expected_code
        assert json.loads(websocket.sent[-1])["type"] == "cancel"


@pytest.mark.parametrize("operation, code, expected, result_model", [
    ("filesystem.edit", "file.editTextNotFound", "file.editTextNotFound", WorkspaceWriteResult),
    ("filesystem.edit", "file.editTextNotUnique", "file.editTextNotUnique", WorkspaceWriteResult),
    ("terminal.open", "request.invalid", "executionAgent.protocolError", TerminalOpenResult),
    ("git.worktree", "operation.cancelled", "executionAgent.outcomeUnknown", WorkspaceWriteResult),
])
async def test_execution_client_maps_remote_errors(monkeypatch, operation, code, expected, result_model) -> None:
    client = ExecutionClient(uuid4(), frozenset({operation}))

    async def request(*_args, **_kwargs):
        return ResponseMessage.model_validate(
            {
                "type": "response",
                "id": uuid4(),
                "ok": False,
                "error": {"code": code},
            }
        )

    monkeypatch.setattr(
        "mato_loop_backend.execution.client.execution_agent_connections.request", request
    )

    with pytest.raises(ExecutionClientError) as exc_info:
        await client.request(operation, {}, result_model)
    assert exc_info.value.code == expected


@pytest.mark.parametrize("directory", [
    "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin",
])
@pytest.mark.parametrize("suffix", ["", "/python3"])
def test_shell_inspection_accepts_only_fixed_toolchain_metadata_locations(directory, suffix) -> None:
    result = ShellInspectResult.model_validate({
        "requiredCategories": ["read"],
        "targets": [
            {"path": directory + suffix, "kind": "toolchain_metadata"},
            {"path": "/dev/null", "kind": "discard"},
        ],
        "analysisComplete": False,
        "classification": "read",
        "readOnly": True,
        "commands": [],
        "inspectionRevision": "a" * 64,
    })
    assert result.classification == "read"
    assert result.analysis_complete is False
    assert [target.path for target in result.targets] == [directory + suffix, "/dev/null"]


@pytest.mark.parametrize("kind", [
    "directory", "input", "output", "executable", "source", "script", "path",
])
def test_shell_inspection_preserves_legacy_target_kinds_and_incomplete_writes(kind) -> None:
    result = ShellInspectResult.model_validate({
        "requiredCategories": ["write"],
        "targets": [{"path": "/outside/target", "kind": kind}],
        "analysisComplete": False,
        "classification": "write",
        "commands": [],
        "inspectionRevision": "a" * 64,
    })
    assert result.targets[0].kind == kind
    assert result.classification == "write"
    assert result.read_only is False
    assert result.targets[0].access is None


@pytest.mark.parametrize("access", ["read", "write"])
def test_shell_inspection_preserves_authoritative_target_access(access) -> None:
    result = ShellInspectResult.model_validate({
        "requiredCategories": ["write"],
        "targets": [{"path": "/outside/target", "kind": "path", "access": access}],
        "analysisComplete": True, "classification": "write", "commands": [],
        "inspectionRevision": "a" * 64,
    })
    assert result.targets[0].access == access


@pytest.mark.parametrize("target", [
    {"path": "/outside/input", "kind": "input", "access": "write"},
    {"path": "/outside/output", "kind": "output", "access": "read"},
])
def test_shell_inspection_rejects_access_inconsistent_with_target_kind(target) -> None:
    with pytest.raises(ValidationError):
        ShellInspectResult.model_validate({
            "requiredCategories": ["write"],
            "targets": [target], "analysisComplete": True, "classification": "write",
            "commands": [], "inspectionRevision": "a" * 64,
        })


@pytest.mark.parametrize("target", [
    {"path": "/tmp/null", "kind": "discard"},
    {"path": "/dev/null/child", "kind": "discard"},
    {"path": "/usr/bin/python3/data", "kind": "toolchain_metadata"},
    {"path": "/usr/local/bin/python3/data", "kind": "toolchain_metadata"},
    {"path": "/usr/bin-extra/python3", "kind": "toolchain_metadata"},
    {"path": "/usr/local", "kind": "toolchain_metadata"},
    {"path": "/opt/bin", "kind": "toolchain_metadata"},
    {"path": "/usr/bin/../etc", "kind": "toolchain_metadata"},
    {"path": "/usr/bin//python3", "kind": "toolchain_metadata"},
    {"path": "/usr/bin/python3", "kind": "future_metadata"},
    {"path": "/usr/bin/python3", "kind": "toolchain_metadata", "access": "read"},
    {"path": "/dev/null", "kind": "discard", "access": "write"},
])
async def test_shell_client_rejects_invalid_special_targets(monkeypatch, target) -> None:
    client = ExecutionClient(uuid4(), frozenset({"shell.inspect"}))

    async def request(*_args, **_kwargs):
        return ResponseMessage.model_validate({
            "type": "response", "id": uuid4(), "ok": True,
            "result": {
                "requiredCategories": ["read"],
                "targets": [target], "analysisComplete": False,
                "classification": "read", "readOnly": True, "commands": [],
                "inspectionRevision": "a" * 64,
            },
        })

    monkeypatch.setattr(
        "mato_loop_backend.execution.client.execution_agent_connections.request", request,
    )
    with pytest.raises(ExecutionClientError) as error:
        await client.request("shell.inspect", {}, ShellInspectResult)
    assert error.value.code == "executionAgent.protocolError"


@pytest.mark.parametrize("overrides", [
    {"readOnly": False},
    {"classification": "write"},
    {"commands": [{"tokens": ["touch", "file"], "classification": "write"}]},
    {"requiredCategories": ["read", "delete"]},
    {"requiredCategories": []},
    {"requiredCategories": ["read", "read"]},
    {"requiredCategories": ["unknown"]},
])
def test_shell_inspection_rejects_inconsistent_classification(overrides) -> None:
    payload = {
        "requiredCategories": ["read"],
        "targets": [], "analysisComplete": False, "classification": "read",
        "readOnly": True, "commands": [], "inspectionRevision": "a" * 64,
    }
    with pytest.raises(ValidationError):
        ShellInspectResult.model_validate({**payload, **overrides})
    payload.pop("readOnly")
    with pytest.raises(ValidationError):
        ShellInspectResult.model_validate(payload)


@pytest.mark.parametrize("operation", ["shell.inspect", "administrator.run", "integration.example.run"])
async def test_missing_result_fields_distinguish_updates_from_invalid_results(monkeypatch, operation):
    class IntegrationResult(BaseModel):
        answer: int

    client = ExecutionClient(uuid4(), frozenset({operation}))
    response = ResponseMessage.model_validate({
        "type": "response", "id": uuid4(), "ok": True,
        "result": {"targets": [], "analysisComplete": True, "classification": "read",
                   "readOnly": True, "commands": [], "inspectionRevision": "a" * 64},
    })
    monkeypatch.setattr(
        "mato_loop_backend.execution.client.execution_agent_connections.request",
        AsyncMock(return_value=response),
    )
    with pytest.raises(ExecutionClientError) as error:
        await client.request(operation, {}, IntegrationResult if operation.startswith("integration.") else ShellInspectResult)
    assert error.value.code == ("executionAgent.protocolError" if operation.startswith("integration.") else "executionAgent.updateRequired")


def test_shell_inspection_accepts_redirect_only_writes() -> None:
    result = ShellInspectResult.model_validate({
        "requiredCategories": ["write"],
        "targets": [{"path": "/work/output", "kind": "output"}],
        "analysisComplete": True, "classification": "write", "commands": [],
        "inspectionRevision": "a" * 64,
    })
    assert result.read_only is False


@pytest.mark.parametrize("whitespace", ["\n", "\t"])
def test_shell_argument_whitespace_is_data_not_executable_or_prefix(whitespace) -> None:
    argument = f"first{whitespace}rm /outside/file"
    result = ShellCommand.model_validate({
        "tokens": ["printf", argument], "prefix": ["printf"], "classification": "read",
    })
    assert result.tokens == ["printf", argument]
    assert ShellCommand.model_validate_json(result.model_dump_json()).tokens == result.tokens
    for overrides in (
        {"tokens": [argument]},
        {"prefix": [argument]},
        {"prefix": ["printf", argument]},
    ):
        with pytest.raises(ValidationError):
            ShellCommand.model_validate({**result.model_dump(), **overrides})


@pytest.mark.parametrize("control", ["\0", "\r", "\x01", "\x1b", "\x7f", "\x80", "\x9f"])
def test_shell_argument_other_controls_remain_rejected(control) -> None:
    with pytest.raises(ValidationError):
        ShellCommand.model_validate({
            "tokens": ["printf", f"first{control}second"], "classification": "read",
        })


@pytest.mark.parametrize("operation", ["shell.inspect", "shell.run", "terminal.run"])
async def test_shell_client_maps_invalid_syntax_without_retry(monkeypatch, operation) -> None:
    client = ExecutionClient(uuid4(), frozenset({operation}))
    calls = []

    async def request(*args, **_kwargs):
        calls.append(args)
        return ResponseMessage.model_validate({
            "type": "response", "id": uuid4(), "ok": False,
            "error": {"code": "process.invalidSyntax"},
        })

    monkeypatch.setattr(
        "mato_loop_backend.execution.client.execution_agent_connections.request", request,
    )
    with pytest.raises(ExecutionClientError) as error:
        await client.request(operation, {}, ShellInspectResult)
    assert error.value.code == "process.invalidSyntax"
    assert len(calls) == 1
