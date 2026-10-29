import asyncio
import json
import wave
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from runtime_helpers import (
    SOURCE_CITATION,
    SOURCE_ID,
    SOURCE_TITLE,
    SOURCE_URL,
    _assistant_messages,
    _runtime_setup,
    _search_call,
    _search_result,
    _tool_messages,
)

import mato_loop_backend.agent.loop as mock_agent_loop
import mato_loop_backend.agent.memory_context as mock_memory_context
import mato_loop_backend.agent.openscad_preview as mock_openscad_preview
import mato_loop_backend.agent.runtime as mock_agent_runtime
import mato_loop_backend.agent.titles as mock_agent_titles
import mato_loop_backend.agent.video_analysis as video_analysis
import mato_loop_backend.memory as memory
import mato_loop_backend.permissions.requests as mock_permissions_requests
from mato_loop_backend.agent.research import _linked_research_source_ids
from mato_loop_backend.agent.run_contracts import (
    AgentRunError,
    ApprovalResolution,
    _VisibleResponse,
)
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.automations import AutomationService
from mato_loop_backend.events import run_events
from mato_loop_backend.execution import (
    ExecutionClient,
    ExecutionClientError,
    WorkspaceReadResult,
    WorkspaceStatResult,
)
from mato_loop_backend.execution.manager import ExecutionAgentConnectionManager
from mato_loop_backend.execution.protocol import HelloMessage, SystemInfo
from mato_loop_backend.files.service import RootFileService
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.media.video_processing import MediaInfo, VideoFrame
from mato_loop_backend.models import (
    ApprovalRequest,
    ExecutionAgent,
    MemoryRecord,
    Message,
    UserAccount,
    WebSearchPolicy,
)
from mato_loop_backend.permissions.policy import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
)
from mato_loop_backend.providers.base import ProviderResult, ToolCall
from mato_loop_backend.providers.service import ProviderService
from mato_loop_backend.settings import (
    BUILD_PROFILE_ID,
    GENERAL_PROFILE_ID,
    AiProviderSettings,
    IntegrationInstance,
    IntegrationSettings,
    MemorySettings,
    SettingsStore,
    SkillDefinition,
    SkillsSettings,
    ToolSettings,
)
from mato_loop_backend.settings.models import (
    ModelInputModality,
    TrustLevel,
)
from mato_loop_backend.tools.catalog import ToolRegistry, tool_registry
from mato_loop_backend.tools.contracts import ToolAttachment, ToolFileAction, ToolResult

pytestmark = pytest.mark.usefixtures("skill_library")


async def test_session_automation_context_refreshes_between_model_requests(monkeypatch) -> None:
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="", tool_calls=[ToolCall("quiet", "silence", {})]), ProviderResult(content="Logged")],
        search_policy=WebSearchPolicy.DISABLED,
        input_budget_tokens=100_000,
    )
    monkeypatch.setattr(AutomationService, "session_definitions", AsyncMock(side_effect=[
        [{"name": "Check", "task": "Watch the job", "revision": 1}],
        [{"name": "Check", "task": "Watch the updated job", "revision": 2}],
    ]))
    await AgentRuntime._execute_loop(db, run)
    assert [
        task in request["messages"][0]["content"]
        for task, request in zip(("Watch the job", "Watch the updated job"), requests, strict=True)
    ] == [True, True]


def _clear_continuation_test_state(run_id) -> None:
    AgentRuntime._tasks.pop(run_id, None)
    AgentRuntime._cancellable_runs.discard(run_id)
    AgentRuntime._user_cancelled_runs.discard(run_id)
    AgentRuntime._redirected_runs.discard(run_id)
    AgentRuntime._tool_active_runs.discard(run_id)
    AgentRuntime._continuations.pop(run_id, None)
    AgentRuntime._pending_continuations.discard(run_id)
    AgentRuntime._provider_active_runs.discard(run_id)


@pytest.mark.parametrize("profile_id,read_available", [
    (BUILD_PROFILE_ID, True), (BUILD_PROFILE_ID, False), (GENERAL_PROFILE_ID, True),
])
@pytest.mark.parametrize("operation", ["read", "write_file", "apply_patch", "move_path", "move_directory", "move_directory_exact_rules", "shell_run", "shell_creates_rules"])
async def test_repository_rules_arrive_before_model_and_follow_successful_reads(monkeypatch, profile_id, read_available, operation) -> None:
    execute = tool_registry._execute_definition
    enabled = profile_id == BUILD_PROFILE_ID and read_available
    mutation = operation != "read"
    creates_rules = operation == "shell_creates_rules"
    deferred = enabled and mutation and not creates_rules
    arguments = {
        "read": {"root": "computer", "path": "src/main.py"},
        "write_file": {"root": "computer", "path": "src/new.py", "content": "pass"},
        "apply_patch": {"patch": "*** Begin Patch\n*** Add File: src/new.py\n+pass\n*** Update File: src/main.py\n*** Move to: dst/main.py\n@@\n-pass\n+pass # moved\n*** End Patch\n",
                        "expectedSha256": {"src/main.py": "a" * 64}},
        "move_path": {"source_root": "computer", "source_path": "src/main.py", "destination_root": "computer", "destination_path": "dst/main.py"},
        "move_directory": {"source_root": "computer", "source_path": "src", "destination_root": "computer", "destination_path": "dst/new"},
        "move_directory_exact_rules": {"source_root": "computer", "source_path": "src", "destination_root": "computer", "destination_path": "dst/new"},
        "shell_run": {"command": "touch src/new.py"},
        "shell_creates_rules": {"command": "printf 'Use backend conventions.' > src/AGENTS.md"},
    }[operation]
    call = ToolCall("source", "move_path" if operation.startswith("move_directory") else "shell_run" if creates_rules else operation, arguments)
    calls = [call, ToolCall("trailing", "write_file", {"root": "computer", "path": "src/other.py", "content": "pass"})] if mutation and not creates_rules else [call]
    results = [ProviderResult(tool_calls=calls)]
    if deferred:
        results.append(ProviderResult(tool_calls=calls))
    results.append(ProviderResult(content="Done"))
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch, results,
        search_policy=WebSearchPolicy.DISABLED, execution_agent_id=uuid4(),
        execution_operations=("filesystem.stat", "filesystem.read", "filesystem.write", "filesystem.patch", "filesystem.move", "shell.inspect", "shell.run"),
        input_budget_tokens=100_000,
    )
    load_settings = SettingsStore.load

    async def load(_db, kind, **kwargs):
        settings = await load_settings(_db, kind, **kwargs)
        if kind is ToolSettings:
            settings.tool_availability["read"] = read_available
        return settings

    monkeypatch.setattr(SettingsStore, "load", load)
    session = db.get.return_value
    session.agent_profile_id = run.agent_profile_id = profile_id
    session.current_directory = run.current_directory_snapshot = "/work"
    agent = ExecutionAgent(id=run.execution_agent_id, user_id=session.user_id, alias="Computer")
    records = AgentRuntime._records.return_value
    db.get.side_effect = lambda model, _id: agent if model is ExecutionAgent else records[0] if model is Message else session
    db.add.side_effect = lambda record: records.append(record) if isinstance(record, Message) else None
    monkeypatch.setattr(tool_registry, "_execute_definition", execute)
    async def authorize(_db, _run, _session, _call, request, **kwargs):
        if (operation == "move_directory_exact_rules" and request.capability == "filesystem.read"
                and json.loads(request.resource_scope).get("path") == "/work/src"):
            return PermissionDecision.ASK
        return PermissionDecision.ALLOW
    monkeypatch.setattr(AgentRuntime, "_authorize", authorize)
    files = {"/work/AGENTS.md": "Use project conventions.", "/work/src/AGENTS.md": "Use backend conventions.",
             "/work/dst/AGENTS.md": "Use destination conventions.", "/work/src/main.py": "pass"}
    if creates_rules:
        files.pop("/work/src/AGENTS.md")

    async def remote(_client, operation, arguments, result_model, **_kwargs):
        if operation == "shell.inspect":
            return result_model.model_validate({"requiredCategories": ["write"], "targets": [{"path": "/work/src/AGENTS.md" if creates_rules else "/work/src/new.py", "kind": "output", "access": "write"}],
                                                "classification": "write", "analysisComplete": True, "commands": [], "inspectionRevision": "b" * 64})
        if operation not in {"filesystem.stat", "filesystem.read"}:
            if enabled and not creates_rules:
                assert "Use backend conventions." in requests[-1]["messages"][0]["content"]
                if operation in {"filesystem.patch", "filesystem.move"}:
                    assert "Use destination conventions." in requests[-1]["messages"][0]["content"]
            if operation == "filesystem.patch":
                return result_model.model_validate({"files": [
                    {"operation": "add", "path": "/work/src/new.py", "sha256": "b" * 64, "size": 4},
                    {"operation": "move", "path": "/work/src/main.py", "destination": "/work/dst/main.py", "sha256": "b" * 64, "size": 12},
                ]})
            if operation == "shell.run":
                if creates_rules:
                    files["/work/src/AGENTS.md"] = "Use backend conventions."
                return result_model.model_validate({"exitCode": 0, "timedOut": False, "durationMs": 1, "stdoutBytes": 0, "stderrBytes": 0,
                                                    "stdoutTruncated": False, "stderrTruncated": False})
            return result_model.model_validate({"path": arguments.get("destination", arguments["path"]), "sha256": "b" * 64, "size": 4})
        path = arguments["path"]
        if operation == "filesystem.stat":
            if path in {"/work/src", "/work/dst"}:
                return WorkspaceStatResult(path=path, kind="directory", size=0, modified_at=0.0)
            if path not in files and path != "/work/.git":
                raise ExecutionClientError("file.pathNotFound")
            return WorkspaceStatResult(path=path, kind="directory" if path.endswith(".git") else "file",
                                       size=len(files.get(path, "")), modified_at=0.0)
        return WorkspaceReadResult(content=files[path], lines_read=1, has_more=False,
                                   truncated_lines=[], sha256="a" * 64)

    monkeypatch.setattr(ExecutionClient, "request", remote)
    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == (3 if deferred else 2)
    assert ("Use project conventions." in requests[0]["messages"][0]["content"]) == enabled
    assert "Use backend conventions." not in requests[0]["messages"][0]["content"]
    assert requests[1]["messages"][0]["content"].count("Use backend conventions.") == int(enabled)
    if deferred:
        assert all(json.loads(message.content)["executed"] is False for message in _tool_messages(db)[:len(calls)])


async def test_completed_response_persists_canonical_markdown(monkeypatch) -> None:
    wrapped = "````markdown\n# Document\n\n```mermaid\ngraph TD\nA --> B\n```\n````"
    expected = "# Document\n\n```mermaid\ngraph TD\nA --> B\n```\n"
    db, run, _requests, _projections, events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content=wrapped, finish_reason="stop")],
        search_policy=WebSearchPolicy.AVAILABLE,
    )

    await AgentRuntime._execute_loop(db, run)

    assert _assistant_messages(db)[0].content == expected
    completed = next(event for event in events if event.get("type") == "message.completed")
    assert completed["message"]["content"] == expected


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("state", ["connected", "offline", "unselected", "foreign", "revoked"])
async def test_runtime_projects_only_selected_connected_computer_system_info(
    monkeypatch, state, delegated,
) -> None:
    agent_id = uuid4()
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch, [ProviderResult(content="Antwort", finish_reason="stop")],
        search_policy=WebSearchPolicy.AVAILABLE,
        execution_agent_id=agent_id if state != "unselected" else None,
    )
    session = db.get.return_value
    if delegated:
        session.parent_session_id = uuid4()
    agent = ExecutionAgent(
        id=agent_id, user_id=uuid4() if state == "foreign" else session.user_id,
        alias="Selected computer", platform="linux",
        revoked_at=datetime.now(UTC) if state == "revoked" else None,
    )
    db.get.side_effect = lambda model, _id, **_: agent if model is ExecutionAgent else session
    _mock_value = AsyncMock(return_value=None)
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'delegation_snapshot', _mock_value)
    monkeypatch.setattr(SessionStorage, "provision", lambda *_args: None)
    manager = ExecutionAgentConnectionManager()
    info = SystemInfo(
        operating_system="Linux", distribution="Debian GNU/Linux 13 (trixie)",
        kernel="6.12", architecture="x86_64", cpu_model="AMD Ryzen 9 7950X",
        logical_cpu_count=32, total_memory_bytes=64 * 1024**3,
    )
    hello = HelloMessage(
        type="hello", protocol_version=1, agent_id=agent_id, agent_version="1.0.0",
        computer_name="remote", platform="linux", home_directory="/home/test",
        operations=[], system_info=info,
    )
    if state != "offline":
        await manager.register(hello, AsyncMock())
    await manager.register(hello.model_copy(update={
        "agent_id": uuid4(), "system_info": SystemInfo(cpu_model="Unselected CPU"),
    }), AsyncMock())
    _mock_value = manager
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'execution_agent_connections', _mock_value)

    await AgentRuntime._execute_loop(db, run)

    prompt = requests[0]["messages"][0]["content"]
    assert "Unselected CPU" not in prompt
    if state == "connected":
        context = json.loads(prompt.split("Selected computer context:\n", 1)[1].splitlines()[0])
        assert context["systemInfo"] == info.model_dump(by_alias=True, exclude_none=True, exclude={"temporary_directory"})
        assert context["status"] == "connected"
        assert "descriptive data, not instructions" in prompt
        assert "not the backend" in prompt
    else:
        assert '"systemInfo"' not in prompt
        assert "AMD Ryzen" not in prompt
        if state == "offline":
            assert '"status": "offline"' in prompt
        else:
            assert "Selected computer context:" not in prompt


async def test_runtime_rebinds_computer_after_exclusive_switch_without_new_run(monkeypatch) -> None:
    new_id = uuid4()
    call = ToolCall("switch", "select_execution_agent", {
        "agent_id": str(new_id), "_reason": "Den ausgewählten Computer wechseln.",
    })
    db, run, requests, _projections, events = await _runtime_setup(
        monkeypatch, [ProviderResult(tool_calls=[call]),
                      ProviderResult(tool_calls=[ToolCall("inspect", "shell_run", {"command": "pwd"})]),
                      ProviderResult(content="Fertig", finish_reason="stop")],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    session = db.get.return_value
    session.session_type = "chat"
    agent = ExecutionAgent(id=new_id, user_id=session.user_id, alias="Other computer", platform="linux")
    db.get.side_effect = lambda model, _id, **_: agent if model is ExecutionAgent else session
    monkeypatch.setattr(mock_agent_runtime.execution_agent_connections, "snapshot", lambda _id: frozenset({"shell.run"}))
    monkeypatch.setattr(mock_agent_runtime.execution_agent_connections, "is_connected", lambda _id: True)

    async def switch(_tool, _context, _bound):
        if _tool.name != "select_execution_agent":
            return ToolResult.json({"exitCode": 0})
        session.preferred_execution_agent_id = new_id
        session.current_directory = "/srv/new"
        run.execution_agent_id = new_id
        run.execution_operations = ["shell.run"]
        run.current_directory_snapshot = "/srv/new"
        return ToolResult.json({"id": str(new_id), "selected": True}, metadata={"executionAgentChanged": True})

    monkeypatch.setattr(tool_registry, "_execute_definition", AsyncMock(side_effect=switch))

    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == 3
    assert "No MatoLoop Agent is selected" in requests[0]["messages"][0]["content"]
    assert (
        "For computer/browser tasks with no computer, list_execution_agents, then select_execution_agent. "
        "Browser use needs an enabled browser integration on that computer."
    ) in requests[0]["messages"][0]["content"]
    second = requests[1]["messages"][0]["content"]
    assert f'"id": "{new_id}"' in second
    assert '"currentDirectory": "/srv/new"' in second
    assert any(tool["name"] == "shell_run" for tool in requests[1]["tools"])
    assert any(event["type"] == "session.executionAgentChanged" for event in events)
    binding = _tool_messages(db)[-1].provider_data["_matoloop"]
    assert (binding["executionAgentId"], binding["executionAgentAlias"]) == (str(new_id), "Other computer")


async def test_runtime_rejects_computer_switch_in_tool_batch(monkeypatch) -> None:
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch, [
            ProviderResult(tool_calls=[
                ToolCall("switch", "select_execution_agent", {"agent_id": str(uuid4())}),
                ToolCall("list", "list_execution_agents", {}),
            ]),
            ProviderResult(content="Fertig", finish_reason="stop"),
        ], search_policy=WebSearchPolicy.AVAILABLE,
    )
    db.get.return_value.session_type = "chat"

    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == 2
    assert tool_registry._execute_definition.await_count == 0
    assert [json.loads(item.content)["error"]["code"] for item in _tool_messages(db)] == [
        "executionAgent.exclusiveCallRequired", "executionAgent.exclusiveCallRequired",
    ]
    assert run.execution_agent_id is None


async def test_session_reasoning_effort_reaches_primary_model_request(monkeypatch) -> None:
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Antwort", finish_reason="stop")],
        search_policy=WebSearchPolicy.AVAILABLE,
        reasoning_effort="low",
    )

    await AgentRuntime._execute_loop(db, run)

    assert requests[0]["reasoning_effort"] == "low"


@pytest.mark.parametrize("computer", [False, True])
async def test_child_reuses_context_and_only_inherited_computer_files(
    monkeypatch, computer,
):
    from mato_loop_backend.agent.subagents import permission_snapshot
    from mato_loop_backend.settings.models import plan_profile

    execution_agent_id = uuid4() if computer else None
    db, run, requests, projections, _events = await _runtime_setup(
        monkeypatch, [ProviderResult(content="Translated text", finish_reason="stop")],
        search_policy=WebSearchPolicy.REQUIRED, reasoning_effort="high",
        execution_agent_id=execution_agent_id,
        execution_operations=("filesystem.read", "filesystem.write") if computer else (),
    )
    run.current_directory_snapshot = "/work"
    session = db.get.return_value
    session.parent_session_id = uuid4()
    session.memory_mode = "full"
    run.trust_level = "public"
    run.input_message_id = uuid4()
    profile = plan_profile()
    run.agent_profile_id = session.agent_profile_id = profile.id
    settings = ToolSettings()
    settings._agent_permissions = profile.public_permissions
    message = Message(id=run.input_message_id, session_id=session.id, role="user", content="Translate text",
                      provider_data={"_matoloop": {"delegationCeiling": permission_snapshot(
                          settings, {"task", "question", "read", "web_search", "write_file", "memory_retrieve"}
                      )}})
    db.get.side_effect = lambda model, _key, **kw: message if model is Message else session
    _mock_value = AsyncMock(return_value=profile)
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'load_agent_profile', _mock_value)
    _mock_value = AsyncMock(return_value=(settings, profile))
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'load_agent_profile_tools', _mock_value)
    provision = Mock(side_effect=AssertionError("Delegated runs must not provision session storage"))
    monkeypatch.setattr(SessionStorage, "provision", provision)
    monkeypatch.setattr(memory.policy, "effective_memory", lambda *_args: memory.policy.EffectiveMemory(True, True, True))
    retrieve = AsyncMock(side_effect=AssertionError("Children must not retrieve automatic memory"))
    monkeypatch.setattr(mock_memory_context.MemoryService, "pinned_context", retrieve)
    await AgentRuntime._execute_loop(db, run)
    tools = {tool["name"] for tool in requests[0]["tools"]}
    assert tools == {"question", "web_search"} | (
        {"read", "write_file"} if computer else set()
    )
    for tool in requests[0]["tools"]:
        root = tool["parameters"].get("properties", {}).get("root")
        if root is not None:
            assert root["enum"] == ["computer"]
    assert requests[0]["reasoning_effort"] == "high"
    assert "delegated task" in projections[0]["system_prompt"]
    assert "Plan workflow:" not in projections[0]["system_prompt"]
    assert "Build workflow:" not in projections[0]["system_prompt"]
    assert "root=files path=plan.md" not in projections[0]["system_prompt"]
    assert "delegationCeiling" not in str(requests[0]["messages"])
    retrieve.assert_not_awaited()
    provision.assert_not_called()


async def test_runtime_replays_exact_tool_calls_without_persisting_them(monkeypatch) -> None:
    arguments = {
        "root": "files",
        "path": "note.txt",
        "oldText": "private old text",
        "newText": "private new text",
    }
    db, run, _requests, projections, events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[ToolCall("edit", "edit_file", arguments)],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Fertig.", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
    )

    await AgentRuntime._execute_loop(db, run)

    assert len(projections) == 2
    assert list(projections[1]["live_tool_calls"].values()) == [
        [{"id": "edit", "name": "edit_file", "arguments": arguments}]
    ]
    persisted_arguments = _assistant_messages(db)[0].tool_calls[0]["arguments"]
    assert persisted_arguments == {
        "root": "files",
        "path": "note.txt",
        "oldText": {"characters": 16, "redacted": True},
        "newText": {"characters": 16, "redacted": True},
    }
    display = _assistant_messages(db)[0].tool_calls[0]["toolDisplay"]
    assert display == {
        "group": "filesystem", "groupLabel": "Dateisystem", "icon": "folder",
        "emoji": "📁", "label": "Datei bearbeiten", "summary": "note.txt",
    }
    assert next(event for event in events if event.get("phase") == "processing_tool")["toolDisplay"] == display
    assert next(event for event in events if event.get("type") == "tool.completed")["toolDisplay"] == display
    assert next(
        event for event in events
        if event.get("type") == "message.completed" and event["message"].get("toolCalls")
    )["message"]["toolCalls"][0]["toolDisplay"] == display


@pytest.mark.parametrize("integration_tool_loading", ["all", "on_demand"])
async def test_generated_openscad_source_requires_a_successful_render(monkeypatch, integration_tool_loading) -> None:
    agent_id = uuid4()
    integration = IntegrationInstance(
        name="OpenSCAD",
        integration_type="openscad",
        adapter_type="openscad",
        execution_agent_id=agent_id,
        config={"binary_path": "/usr/bin/openscad"},
    )
    db, run, requests, projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(content="```scad\ncube(1);\n```", finish_reason="stop"),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="write",
                        name="write_file",
                        arguments={
                            "root": "session",
                            "path": "cube.scad",
                            "content": "cube(1);\n",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="export",
                        name="render_openscad",
                        arguments={
                            "root": "session",
                            "path": "cube.scad",
                            "output": "stl",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="render",
                        name="render_openscad",
                        arguments={
                            "root": "session",
                            "path": "cube.scad",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="wrong-review",
                        name="read",
                        arguments={
                            "root": "session",
                            "path": "other.scad",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="review",
                        name="read",
                        arguments={
                            "root": "session",
                            "path": "cube.scad",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Fertig gerendert.", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        integration_settings=IntegrationSettings(instances=[integration]),
        integration_tool_loading=integration_tool_loading,
        execution_agent_id=agent_id,
        execution_operations=("integration.openscad.render", "cache.export"),
    )

    await AgentRuntime._execute_loop(db, run)

    assert [request["tool_choice"] for request in requests] == [
        "auto",
        "required",
        "required",
        "required",
        "required",
        "required",
        "auto",
    ]
    assert "has not been rendered successfully" in projections[1]["system_prompt"]
    assert "must be reviewed before finalizing" in projections[4]["system_prompt"]
    assert not any(
        "cube(1)" in message.content for message in _assistant_messages(db)
    )
    assert _assistant_messages(db)[-1].content == "Fertig gerendert."


async def test_execution_agent_unavailable_stops_before_model_retry(monkeypatch) -> None:
    agent_id = uuid4()
    integration = IntegrationInstance(
        name="OpenSCAD",
        integration_type="openscad",
        adapter_type="openscad",
        execution_agent_id=agent_id,
        config={"binary_path": "/usr/bin/openscad"},
    )
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="render",
                        name="render_openscad",
                        arguments={
                            "root": "session",
                            "path": "model.scad",
                            "output": "preview",
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        integration_settings=IntegrationSettings(instances=[integration]),
        execution_agent_id=agent_id,
        execution_operations=("integration.openscad.render", "cache.export"),
        tool_result=ToolResult.error("executionAgent.unavailable"),
    )

    with pytest.raises(AgentRunError) as exc_info:
        await AgentRuntime._execute_loop(db, run)

    assert exc_info.value.code == "executionAgent.unavailable"
    assert len(requests) == 1
    assert tool_registry._execute_definition.await_count == 1


async def test_openscad_contact_sheet_is_required_for_active_vision_model(monkeypatch) -> None:
    agent_id = uuid4()
    integration = IntegrationInstance(
        name="OpenSCAD",
        integration_type="openscad",
        adapter_type="openscad",
        execution_agent_id=agent_id,
        config={"binary_path": "/usr/bin/openscad"},
    )
    db, run, requests, projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="write",
                        name="write_file",
                        arguments={
                            "root": "session",
                            "path": "model.scad",
                            "content": "cube(1);",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="render",
                        name="render_openscad",
                        arguments={
                            "root": "session",
                            "path": "model.scad",
                            "output": "preview",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                content=json.dumps(
                    {
                        "verdict": "pass",
                        "findings": ["No defect."],
                        "uncertainty": ["Rear geometry is hidden."],
                    }
                ),
                finish_reason="stop",
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="review",
                        name="read",
                        arguments={
                            "root": "session",
                            "path": "model.scad",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Visuell verglichen.", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        active_supports_image_input=True,
        integration_settings=IntegrationSettings(instances=[integration]),
        execution_agent_id=agent_id,
        execution_operations=("integration.openscad.render", "cache.export"),
        user_image=True,
        downloaded_image=True,
        safety_reserve_tokens=128,
    )
    generated_id = uuid4()

    class FakeAttachmentService:
        def __init__(self, **_kwargs):
            pass

        async def create_attachment(self, _db, _message, _data, _mime_type, **kwargs):
            return SimpleNamespace(id=generated_id, kind=kwargs["kind"])

        async def read_image(self, attachment, _session_id, *, current_only=False):
            assert current_only is True
            return (attachment.filename or "image").encode()

    _mock_value = FakeAttachmentService
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles, mock_openscad_preview):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    monkeypatch.setattr(
        tool_registry,
        "_execute_definition",
        AsyncMock(
            side_effect=[
                ToolResult.json({"written": "model.scad"}),
                ToolResult(
                    json.dumps(
                        {
                            "output": "preview",
                            "previewLayout": ["isometric", "front", "right", "top"],
                            "geometryMetadata": {"triangleCount": 12},
                        }
                    ),
                    {},
                    attachments=(
                        ToolAttachment(b"png", "image/png", "model-views.png"),
                    ),
                ),
                ToolResult.json({"content": "cube(1);"}),
            ]
        ),
    )

    await AgentRuntime._execute_loop(db, run)

    assert [request["model"].model_id for request in requests] == [
        "local-model",
        "local-model",
        "local-model",
        "local-model",
        "local-model",
    ]
    verifier = requests[2]
    assert verifier["tools"] == []
    assert verifier["tool_choice"] == "none"
    assert verifier["max_output_tokens"] == 512
    assert verifier["safety_reserve_tokens"] == 128
    assert [image.data for image in verifier["image_inputs"]] == [
        b"png",
        b"reference-0.png",
        b"drawing-page-2.jpg",
    ]
    verifier_prompt = verifier["messages"][0]["content"]
    assert '"verdict":"pass|fail|uncertain"' in verifier_prompt
    assert "Pass requires an empty uncertainty list" in verifier_prompt
    assert "Do not infer hidden geometry" in verifier_prompt
    verifier_data = json.loads(verifier["messages"][1]["content"])
    assert verifier_data == {
        "imageOrder": {"contactSheet": 1, "referenceImages": [2, 3]},
        "recentRequests": [
            {
                "messageId": verifier_data["recentRequests"][0]["messageId"],
                "content": "Aktuelle Frage",
            }
        ],
        "previewLayout": ["isometric", "front", "right", "top"],
        "geometryMetadata": {"triangleCount": 12},
        "referenceImages": [
            {"image": 2, "filename": "reference-0.png"},
            {
                "image": 3,
                "filename": "drawing-page-2.jpg",
                "pageNumber": 2,
            },
        ],
    }
    assert requests[3]["required_image_ids"] == ()
    assert "required_image_ids" not in projections[2]
    render_message = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message)
        and call.args[0].role == "tool"
        and call.args[0].tool_call_id == "render"
    )
    assert json.loads(render_message.content)["visualVerification"] == {
        "status": "completed",
        "scope": "visible_geometry",
        "verdict": "uncertain",
        "findings": ["No defect."],
        "uncertainty": ["Rear geometry is hidden."],
        "referenceImageCount": 2,
    }
    assert _assistant_messages(db)[-1].content == "Visuell verglichen."


@pytest.mark.parametrize("vision_fallback", [False, True])
async def test_text_model_marks_or_runs_openscad_visual_verification(
    monkeypatch, vision_fallback: bool
) -> None:
    agent_id = uuid4()
    integration = IntegrationInstance(
        name="OpenSCAD",
        integration_type="openscad",
        adapter_type="openscad",
        execution_agent_id=agent_id,
        config={"binary_path": "/usr/bin/openscad"},
    )
    results = [
        ProviderResult(
            tool_calls=[
                ToolCall(
                    id="write",
                    name="write_file",
                    arguments={
                        "root": "session",
                        "path": "model.scad",
                        "content": "cube(1);",
                    },
                )
            ],
            finish_reason="tool_calls",
        ),
        ProviderResult(
            tool_calls=[
                ToolCall(
                    id="render",
                    name="render_openscad",
                    arguments={"root": "session", "path": "model.scad"},
                )
            ],
            finish_reason="tool_calls",
        ),
    ]
    if vision_fallback:
        results.append(
            ProviderResult(
                content=json.dumps(
                    {
                        "verdict": "pass",
                        "findings": ["Geometry matches."],
                        "uncertainty": [],
                    }
                ),
                finish_reason="stop",
            )
        )
    results.append(
        ProviderResult(
            tool_calls=[
                ToolCall(
                    id="review",
                    name="read",
                    arguments={
                        "root": "session",
                        "path": "model.scad",
                    },
                )
            ],
            finish_reason="tool_calls",
        )
    )
    results.append(ProviderResult(content="Fertig.", finish_reason="stop"))
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        results,
        search_policy=WebSearchPolicy.AVAILABLE,
        vision_fallback=vision_fallback,
        integration_settings=IntegrationSettings(instances=[integration]),
        execution_agent_id=agent_id,
        execution_operations=("integration.openscad.render", "cache.export"),
        user_image=True,
        user_image_count=8,
        user_content="x" * 5_000,
    )
    generated_id = uuid4()

    class FakeAttachmentService:
        def __init__(self, **_kwargs):
            pass

        async def create_attachment(self, _db, _message, _data, _mime_type, **kwargs):
            return SimpleNamespace(id=generated_id, kind=kwargs["kind"])

        async def read_image(self, attachment, _session_id, *, current_only=False):
            assert current_only is True
            return f"reference-{attachment.position}".encode()

    _mock_value = FakeAttachmentService
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles, mock_openscad_preview):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    monkeypatch.setattr(
        tool_registry,
        "_execute_definition",
        AsyncMock(
            side_effect=[
                ToolResult.json({"written": "model.scad"}),
                ToolResult(
                    json.dumps(
                        {
                            "output": "preview",
                            "previewLayout": ["isometric", "front", "right", "top"],
                            "geometryMetadata": {"triangleCount": 6},
                        }
                    ),
                    {},
                    attachments=(ToolAttachment(b"png", "image/png", "model-views.png"),),
                ),
                ToolResult.json({"content": "cube(1);"}),
            ]
        ),
    )

    await AgentRuntime._execute_loop(db, run)

    assert [request["model"].model_id for request in requests] == (
        ["local-model", "local-model", "vision-model", "local-model", "local-model"]
        if vision_fallback
        else ["local-model", "local-model", "local-model", "local-model"]
    )
    if vision_fallback:
        assert [image.data for image in requests[2]["image_inputs"]] == [
            b"png",
            *(f"reference-{position}".encode() for position in range(7)),
        ]
        verifier_data = json.loads(requests[2]["messages"][1]["content"])
        assert verifier_data["imageOrder"] == {
            "contactSheet": 1,
            "referenceImages": [2, 3, 4, 5, 6, 7, 8],
        }
        assert len(verifier_data["recentRequests"][0]["content"]) == 4_000
        assert verifier_data["previewLayout"] == [
            "isometric",
            "front",
            "right",
            "top",
        ]
        assert verifier_data["geometryMetadata"] == {"triangleCount": 6}
        assert verifier_data["referenceImages"] == [
            {"image": position + 2, "filename": f"reference-{position}.png"}
            for position in range(7)
        ]
    tool_message = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message)
        and call.args[0].role == "tool"
        and call.args[0].tool_call_id == "render"
    )
    verification = json.loads(tool_message.content)["visualVerification"]
    assert verification == (
        {
            "status": "completed",
            "scope": "visible_geometry",
            "verdict": "pass",
            "findings": ["Geometry matches."],
            "uncertainty": [],
            "referenceImageCount": 7,
        }
        if vision_fallback
        else {"status": "unavailable"}
    )


async def test_private_openscad_source_uses_public_active_vision_model(monkeypatch) -> None:
    agent_id = uuid4()
    integration = IntegrationInstance(
        name="OpenSCAD",
        integration_type="openscad",
        adapter_type="openscad",
        execution_agent_id=agent_id,
        config={"binary_path": "/usr/bin/openscad"},
    )
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="render",
                        name="render_openscad",
                        arguments={"root": "session", "path": "model.scad"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                content=json.dumps(
                    {
                        "verdict": "pass",
                        "findings": ["None visible."],
                        "uncertainty": [],
                    }
                )
            ),
            ProviderResult(content="Fertig.", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        active_supports_image_input=True,
        vision_fallback=True,
        integration_settings=IntegrationSettings(instances=[integration]),
        execution_agent_id=agent_id,
        execution_operations=("integration.openscad.render", "cache.export"),
        user_trust_level=TrustLevel.PRIVATE,
    )

    class FakeAttachmentService:
        def __init__(self, **_kwargs):
            pass

        async def create_attachment(self, _db, _message, _data, _mime_type, **kwargs):
            return SimpleNamespace(id=uuid4(), kind=kwargs["kind"])

    _mock_value = FakeAttachmentService
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    monkeypatch.setattr(
        tool_registry,
        "_execute_definition",
        AsyncMock(
            return_value=ToolResult(
                '{"output":"preview"}',
                {},
                attachments=(ToolAttachment(b"png", "image/png", "model-views.png"),),
            )
        ),
    )

    await AgentRuntime._execute_loop(db, run)

    assert [request["model"].model_id for request in requests] == [
        "local-model",
        "local-model",
        "local-model",
    ]
    assert requests[1]["target_trust_level"] == TrustLevel.PUBLIC
    assert requests[1]["image_inputs"][0].trust_level == TrustLevel.PRIVATE.value


async def test_failed_active_openscad_verifier_does_not_fallback_and_counts_budget(
    monkeypatch,
) -> None:
    agent_id = uuid4()
    integration = IntegrationInstance(
        name="OpenSCAD",
        integration_type="openscad",
        adapter_type="openscad",
        execution_agent_id=agent_id,
        config={"binary_path": "/usr/bin/openscad"},
    )
    db, run, requests, _projections, events = await _runtime_setup(
        monkeypatch,
        [],
        search_policy=WebSearchPolicy.AVAILABLE,
        active_supports_image_input=True,
        vision_fallback=True,
        integration_settings=IntegrationSettings(instances=[integration]),
        execution_agent_id=agent_id,
        execution_operations=("integration.openscad.render", "cache.export"),
    )
    generated_id = uuid4()

    class FakeAttachmentService:
        def __init__(self, **_kwargs):
            pass

        async def create_attachment(self, _db, _message, _data, _mime_type, **kwargs):
            return SimpleNamespace(id=generated_id, kind=kwargs["kind"])

    async def complete(_db, **kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            return ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="render",
                        name="render_openscad",
                        arguments={"root": "session", "path": "model.scad"},
                    )
                ],
                finish_reason="tool_calls",
            )
        if len(requests) == 2:
            raise RuntimeError("vision unavailable")
        return ProviderResult(content="Fertig.", finish_reason="stop")

    _mock_value = FakeAttachmentService
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    monkeypatch.setattr(ProviderService, "complete", staticmethod(complete))
    monkeypatch.setattr(
        tool_registry,
        "_execute_definition",
        AsyncMock(
            return_value=ToolResult(
                '{"output":"preview"}',
                {},
                attachments=(ToolAttachment(b"png", "image/png", "model-views.png"),),
            )
        ),
    )

    await AgentRuntime._execute_loop(db, run)

    assert [request["model"].model_id for request in requests] == [
        "local-model",
        "local-model",
        "local-model",
    ]
    assert requests[1]["tool_choice"] == "none"
    assert requests[2]["required_image_ids"] == ()
    assert [
        event["modelRequest"]
        for event in events
        if event.get("type") == "run.phase" and event.get("phase") == "waiting_model"
    ] == [1, 3]
    internal_phase = next(event for event in events if event.get("modelRequest") == 2)
    assert internal_phase["phase"] == "processing_tool"
    assert internal_phase["toolCallId"] == "render"
    render_message = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message)
        and call.args[0].role == "tool"
        and call.args[0].tool_call_id == "render"
    )
    assert json.loads(render_message.content)["visualVerification"] == {
        "status": "unavailable"
    }


async def test_each_successful_openscad_preview_runs_a_verifier(monkeypatch) -> None:
    agent_id = uuid4()
    integration = IntegrationInstance(
        name="OpenSCAD",
        integration_type="openscad",
        adapter_type="openscad",
        execution_agent_id=agent_id,
        config={"binary_path": "/usr/bin/openscad"},
    )
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="render-1",
                        name="render_openscad",
                        arguments={"root": "session", "path": "model.scad"},
                    )
                ]
            ),
            ProviderResult(
                content=json.dumps(
                    {
                        "verdict": "pass",
                        "findings": ["First inspection."],
                        "uncertainty": [],
                    }
                )
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="render-2",
                        name="render_openscad",
                        arguments={"root": "session", "path": "model.scad"},
                    )
                ]
            ),
            ProviderResult(content="Second inspection"),
            ProviderResult(content="Fertig.", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        vision_fallback=True,
        integration_settings=IntegrationSettings(instances=[integration]),
        execution_agent_id=agent_id,
        execution_operations=("integration.openscad.render", "cache.export"),
    )

    class FakeAttachmentService:
        def __init__(self, **_kwargs):
            pass

        async def create_attachment(self, _db, _message, _data, _mime_type, **kwargs):
            return SimpleNamespace(id=uuid4(), kind=kwargs["kind"])

    _mock_value = FakeAttachmentService
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    monkeypatch.setattr(
        tool_registry,
        "_execute_definition",
        AsyncMock(
            side_effect=[
                ToolResult(
                    '{"output":"preview"}',
                    {},
                    attachments=(ToolAttachment(b"one", "image/png", "one.png"),),
                ),
                ToolResult(
                    '{"output":"preview"}',
                    {},
                    attachments=(ToolAttachment(b"two", "image/png", "two.png"),),
                ),
            ]
        ),
    )

    await AgentRuntime._execute_loop(db, run)

    assert [request["model"].model_id for request in requests] == [
        "local-model",
        "vision-model",
        "local-model",
        "vision-model",
        "local-model",
    ]
    assert [request["image_inputs"][0].data for request in (requests[1], requests[3])] == [
        b"one",
        b"two",
    ]
    verifications = [
        json.loads(call.args[0].content)["visualVerification"]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message)
        and call.args[0].role == "tool"
        and call.args[0].tool_call_id in {"render-1", "render-2"}
    ]
    assert verifications == [
        {
            "status": "completed",
            "scope": "visible_geometry",
            "verdict": "pass",
            "findings": ["First inspection."],
            "uncertainty": [],
            "referenceImageCount": 0,
        },
        {"status": "unavailable"},
    ]


async def test_openscad_stl_and_failed_preview_do_not_run_verifier(monkeypatch) -> None:
    agent_id = uuid4()
    integration = IntegrationInstance(
        name="OpenSCAD",
        integration_type="openscad",
        adapter_type="openscad",
        execution_agent_id=agent_id,
        config={"binary_path": "/usr/bin/openscad"},
    )
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="stl",
                        name="render_openscad",
                        arguments={
                            "root": "session",
                            "path": "model.scad",
                            "output": "stl",
                        },
                    ),
                    ToolCall(
                        id="failed-preview",
                        name="render_openscad",
                        arguments={"root": "session", "path": "model.scad"},
                    ),
                ]
            ),
            ProviderResult(content="Fertig.", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        vision_fallback=True,
        integration_settings=IntegrationSettings(instances=[integration]),
        execution_agent_id=agent_id,
        execution_operations=("integration.openscad.render", "cache.export"),
    )
    monkeypatch.setattr(
        tool_registry,
        "_execute_definition",
        AsyncMock(
            side_effect=[
                ToolResult.json({"output": "stl"}),
                ToolResult.error("tool.executionFailed"),
            ]
        ),
    )

    await AgentRuntime._execute_loop(db, run)

    assert [request["model"].model_id for request in requests] == [
        "local-model",
        "local-model",
    ]
    assert all(request["tool_choice"] != "none" for request in requests)


async def test_provider_retry_publishes_waiting_model_phase(monkeypatch) -> None:
    db, run, requests, _projections, events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Fertig", finish_reason="stop")],
        search_policy=WebSearchPolicy.AVAILABLE,
    )

    await AgentRuntime._execute_loop(db, run)
    events.clear()
    await requests[0]["on_retry"](2, 3, 503, "provider.requestFailed")

    started_at = events[0].pop("startedAt")
    assert isinstance(started_at, str)
    assert events == [
        {
            "type": "run.phase",
            "phase": "waiting_model",
            "activityLabel": None,
            "modelRequest": 1,
            "providerAttempt": 2,
            "providerAttemptTotal": 3,
        }
    ]


async def test_reasoning_summary_label_remains_after_content_updates(
    monkeypatch,
) -> None:
    db, run, requests, _projections, events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Fertig", finish_reason="stop")],
        search_policy=WebSearchPolicy.AVAILABLE,
    )

    await AgentRuntime._execute_loop(db, run)
    events.clear()
    on_reasoning = requests[0]["on_reasoning"]

    await on_reasoning("**", "summary")
    await on_reasoning("Plan prüfen**", "summary")
    await asyncio.sleep(0.11)
    await on_reasoning("Weitere Überlegung", "content")

    assert [
        event["activityLabel"]
        for event in events
        if event.get("type") == "reasoning.updated"
    ] == ["Nachdenken", "Plan prüfen", "Plan prüfen"]


async def test_required_search_loads_matching_skill_before_search(monkeypatch) -> None:
    skill = SkillDefinition(
        name="current-research",
        description="Use for current web research.",
        instructions="Search official sources first.",
    )
    db, run, requests, projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="load-skill",
                        name="load_skill",
                        arguments={"skill_id": str(skill.id)},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[_search_call("search-after-skill", "Aktuelle Frage")],
                finish_reason="tool_calls",
            ),
            ProviderResult(content=f"Antwort {SOURCE_CITATION}", finish_reason="stop"),
        ],
        skills=[skill],
    )

    async def execute(call, context, bound_tool):
        if call.name == "load_skill":
            return await ToolRegistry._execute_definition(
                tool_registry, call, context, bound_tool
            )
        return _search_result()

    tool_registry._execute_definition.side_effect = execute

    await AgentRuntime._execute_loop(db, run)

    assert {tool["name"] for tool in requests[0]["tools"]} == {
        "load_skill",
        "edit_skill",
        "web_search",
    }
    assert [tool["name"] for tool in requests[1]["tools"]] == ["web_search"]
    assert skill.instructions not in projections[0]["system_prompt"]
    assert skill.instructions in projections[1]["system_prompt"]


async def test_repeated_skill_loads_do_not_satisfy_required_search(
    monkeypatch,
) -> None:
    skill = SkillDefinition(
        name="current-research",
        description="Use for current web research.",
        instructions="Search official sources first.",
    )
    stale = ToolCall(
        id="stale-load",
        name="load_skill",
        arguments={"skill_id": str(skill.id)},
    )
    db, run, requests, _projections, events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="initial-load",
                        name="load_skill",
                        arguments={"skill_id": str(skill.id)},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(tool_calls=[stale], finish_reason="tool_calls"),
            ProviderResult(tool_calls=[stale], finish_reason="tool_calls"),
            ProviderResult(tool_calls=[stale], finish_reason="tool_calls"),
        ],
        skills=[skill],
    )

    async def execute(call, context, bound_tool):
        return await ToolRegistry._execute_definition(
            tool_registry, call, context, bound_tool
        )

    tool_registry._execute_definition.side_effect = execute

    with pytest.raises(AgentRunError) as exc_info:
        await AgentRuntime._execute_loop(db, run)

    assert exc_info.value.code == "agent.requiredWebSearchMissing"
    assert len(requests) == 4
    tool_registry._execute_definition.assert_awaited_once()
    persisted = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message)
    ]
    assert [message.role for message in persisted] == ["assistant", "tool"]
    assert "stale-load" not in str([message.tool_calls for message in persisted])
    assert all(event.get("toolCallId") != "stale-load" for event in events)


async def test_invalid_skill_loads_consume_required_search_retries(monkeypatch) -> None:
    skill = SkillDefinition(
        name="current-research",
        description="Use for current web research.",
        instructions="Search official sources first.",
    )
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id=f"invalid-load-{index}",
                        name="load_skill",
                        arguments={"skill_id": str(uuid4())},
                    )
                ],
                finish_reason="tool_calls",
            )
            for index in range(3)
        ],
        skills=[skill],
    )

    async def execute(call, context, bound_tool):
        return await ToolRegistry._execute_definition(
            tool_registry, call, context, bound_tool
        )

    tool_registry._execute_definition.side_effect = execute

    with pytest.raises(AgentRunError) as exc_info:
        await AgentRuntime._execute_loop(db, run)

    assert exc_info.value.code == "agent.requiredWebSearchMissing"
    assert len(requests) == 3
    assert tool_registry._execute_definition.await_count == 3


async def test_load_skill_is_rejected_when_called_with_another_tool(monkeypatch) -> None:
    skill = SkillDefinition(
        name="current-research",
        description="Use for current web research.",
        instructions="Search official sources first.",
    )
    db, run, _requests, projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="load-skill",
                        name="load_skill",
                        arguments={"skill_id": str(skill.id)},
                    ),
                    _search_call("parallel-search", "Aktuelle Frage"),
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Ohne Skill fortgesetzt.", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        skills=[skill],
    )

    await AgentRuntime._execute_loop(db, run)

    tool_messages = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message) and call.args[0].role == "tool"
    ]
    assert len(tool_messages) == 2
    assert all("skill.loadMustBeExclusive" in item.content for item in tool_messages)
    assert skill.instructions not in projections[1]["system_prompt"]
    tool_registry._execute_definition.assert_not_awaited()


async def test_skill_authoring_uses_normal_file_tools_and_preserves_source_bytes(monkeypatch, tmp_path):
    from mato_loop_backend.skill_packages import SkillPackage, skill_md
    from mato_loop_backend.skills import SkillService

    skills = SkillsSettings()
    calls = [
        ToolCall("package", "create_directory", {"root": "files", "path": "api-skill"}),
        ToolCall("directory", "create_directory", {"root": "files", "path": "api-skill/references"}),
        ToolCall("instructions", "write_file", {"root": "files", "path": "api-skill/SKILL.md", "content": skill_md("api-skill", "Use for application API requests.", "Read references/openapi.json before calling the API.").decode()}),
        ToolCall("reference", "copy_path", {"source_root": "files", "source_path": "openapi.json", "destination_root": "files", "destination_path": "api-skill/references/openapi.json"}),
        ToolCall("publish", "save_skill", {"path": "api-skill"}),
    ]
    original_execute = ToolRegistry._execute_definition
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(tool_calls=[call], finish_reason="tool_calls") for call in calls]
        + [ProviderResult(content="Skill erstellt.", finish_reason="stop")],
        search_policy=WebSearchPolicy.AVAILABLE, skills=skills,
        user_content="Erstelle einen Skill mit der heruntergeladenen openapi.json als Referenz.",
    )
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(run.session_id)
    files = RootFileService(run.session_id, session_storage=storage)
    source = json.dumps({"openapi": "3.1.0", "description": "API reference " * 5000})
    files.write_file("session", "openapi.json", source)
    monkeypatch.setattr(mock_agent_loop, "RootFileService", lambda session_id, **kwargs: RootFileService(session_id, session_storage=storage, run_id=run.id))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    monkeypatch.setattr(AgentRuntime, "_authorize", AsyncMock(return_value=PermissionDecision.ALLOW))

    async def execute(call, context, bound_tool):
        return await original_execute(tool_registry, call, context, bound_tool)

    tool_registry._execute_definition.side_effect = execute
    await AgentRuntime._execute_loop(db, run)

    errors = [message.content for message in _tool_messages(db) if "error" in json.loads(message.content)]
    assert not errors
    saved = await SkillService.get(db, db.get.return_value.user_id, skills.skills[0].id, "personal")
    package = await SkillService.package(saved)
    assert SkillPackage.import_file(package.archive(), archive=True).files["references/openapi.json"].content == source.encode()
    assert len(requests) == len(calls) + 1
    assert all(request["model"].model_id == "local-model" for request in requests)


async def test_runtime_persists_typed_file_actions_on_tool_results(monkeypatch) -> None:
    execution_agent_id = str(uuid4())
    action = ToolFileAction(
        "write",
        "workspace",
        None,
        "/home/user/project/report.txt",
        "report.txt",
        7,
        execution_agent_id,
        "/home/user/project",
    )
    db, run, *_ = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="write-1",
                        name="write_file",
                        arguments={
                            "root": "session",
                            "path": "report.txt",
                            "content": "report",
                        },
                    )
                ]
            ),
            ProviderResult(content="Fertig", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        tool_result=ToolResult.json(
            {"root": "session", "written": "report.txt"},
            file_actions=(action,),
        ),
    )

    await AgentRuntime._execute_loop(db, run)

    tool_message = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message) and call.args[0].role == "tool"
    )
    assert tool_message.provider_data == {
        "_matoloop": {
            "runId": str(run.id),
            "toolName": "write_file",
            "executionAgentId": None,
            "executionAgentAlias": None,
            "fileOutputsPending": True,
            "fileActions": [
                {
                    "operation": "write",
                    "root": "workspace",
                    "path": "/home/user/project/report.txt",
                    "filename": "report.txt",
                    "sizeBytes": 7,
                    "kind": "file",
                    "executionAgentId": execution_agent_id,
                    "currentDirectory": "/home/user/project",
                }
            ]
        }
    }
    assert "download" not in str(tool_message.provider_data).casefold()


async def test_cancelled_dispatched_workspace_write_stops_without_waiting(
    monkeypatch,
) -> None:
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="write-1",
                        name="write_file",
                        arguments={
                            "root": "workspace",
                            "path": "a.txt",
                            "content": "x",
                        },
                    )
                ]
            ),
            ProviderResult(content="Must not be requested", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    run.execution_agent_id = uuid4()
    run.current_directory_snapshot = "/home/user/project"
    run.execution_operations = ["filesystem.write"]
    dispatched = asyncio.Event()

    async def request(
        _agent_id,
        _operation,
        _arguments,
        **_kwargs,
    ):
        dispatched.set()
        await asyncio.Future()

    original_execute = ToolRegistry._execute_definition

    async def execute(call, context, bound_tool):
        return await original_execute(tool_registry, call, context, bound_tool)

    monkeypatch.setattr(
        "mato_loop_backend.execution.client.execution_agent_connections.request",
        request,
    )
    monkeypatch.setattr(
        AgentRuntime, "_authorize", AsyncMock(return_value=PermissionDecision.ALLOW)
    )
    tool_registry._execute_definition.side_effect = execute

    task = asyncio.create_task(AgentRuntime._execute_loop(db, run))
    await dispatched.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not any(
        isinstance(call.args[0], Message) and call.args[0].role == "tool"
        for call in db.add.call_args_list
    )
    assert len(requests) == 1


def test_linked_research_sources_ignore_hidden_and_foreign_links() -> None:
    parenthesized_url = "https://example.test/wiki/Product_(series)"
    content = (
        f"[Beleg]({parenthesized_url})\n"
        "[Andere Quelle](https://other.example/source)\n"
        f"`[Versteckt]({SOURCE_URL})`"
    )

    assert _linked_research_source_ids(
        content,
        {"parenthesized": parenthesized_url, SOURCE_ID: SOURCE_URL},
    ) == ["parenthesized"]


def test_linked_research_sources_support_autolinks_but_not_images() -> None:
    image_url = "https://example.test/image"
    escaped_url = "https://example.test/escaped"
    hidden_url = "https://example.test/hidden"
    content = (
        f"<{SOURCE_URL}>\n"
        f"![Bild]({image_url})\n"
        f"\\[Kein Link]({escaped_url})\n"
        f"<!-- [Versteckt]({hidden_url})"
    )

    assert _linked_research_source_ids(
        content,
        {
            SOURCE_ID: SOURCE_URL,
            "image": image_url,
            "escaped": escaped_url,
            "hidden": hidden_url,
        },
    ) == [SOURCE_ID]


def test_linked_research_sources_match_rendered_destinations_not_link_labels() -> None:
    other_url = "https://other.example/source"
    content = (
        f"[{SOURCE_URL}]({other_url}) "
        f"[Quelle mit Titel]({SOURCE_URL} \"Offiziell\")"
    )

    assert _linked_research_source_ids(
        content,
        {SOURCE_ID: SOURCE_URL, "other": other_url},
    ) == ["other", SOURCE_ID]


async def test_direct_message_restarts_only_model_request_inside_same_run(monkeypatch) -> None:
    db, run, requests, _, events = await _runtime_setup(
        monkeypatch,
        [],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    started = asyncio.Event()

    async def complete(_db, **kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            await kwargs["on_delta"]("Partial answer")
            started.set()
            await asyncio.Future()
        return ProviderResult(content="Updated answer", finish_reason="stop")

    monkeypatch.setattr(ProviderService, "complete", staticmethod(complete))
    db.scalars.return_value = SimpleNamespace(all=lambda: [])
    task = asyncio.create_task(AgentRuntime._execute_loop(db, run))
    AgentRuntime._tasks[run.id] = task
    AgentRuntime._cancellable_runs.add(run.id)
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        reservation = AgentRuntime.reserve_continuation(run.id)
        assert reservation is not None
        records = AgentRuntime._records.return_value
        records.append(Message(id=uuid4(), session_id=run.session_id, role="user", content="Change course"))
        AgentRuntime.finish_continuation(run.id, reservation, accepted=True)
        await asyncio.wait_for(task, timeout=5)

        assert len(requests) == 2
        assert "Change course" in str(requests[-1]["messages"])
        assert [item.content for item in _assistant_messages(db)] == ["Partial answer", "Updated answer"]
        assert [item["message"]["runId"] for item in events if item["type"] == "message.completed"] == [str(run.id), str(run.id)]
    finally:
        _clear_continuation_test_state(run.id)


async def test_direct_message_reloads_expired_memories_after_rollback(monkeypatch) -> None:
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch, [], search_policy=WebSearchPolicy.AVAILABLE
    )
    db.get.return_value.memory_mode = "full"
    db.scalars.return_value = SimpleNamespace(all=lambda: [])
    load_settings = SettingsStore.load

    async def load(_db, settings_type, **kwargs):
        if settings_type is MemorySettings:
            return MemorySettings(enabled=True, automatic_memory_snippet_limit=1,
                                  automatic_session_snippet_limit=0, recent_session_snippet_limit=0)
        return await load_settings(_db, settings_type, **kwargs)

    monkeypatch.setattr(SettingsStore, "load", load)
    def _mock_value():
        return SimpleNamespace(embedding_enabled=False)
    for _mock_target in (mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'get_runtime_config', _mock_value)
    monkeypatch.setattr(mock_memory_context.MemoryService, "scope", AsyncMock(return_value=SimpleNamespace(total=0, pinned=0)))
    monkeypatch.setattr(mock_memory_context.MemoryService, "pinned_context", AsyncMock(return_value=[]))
    memories = [
        MemoryRecord(id=uuid4(), user_id=db.get.return_value.user_id, category="fact",
                     content="Useful context", revision=1, created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        for _ in range(2)
    ]
    retrieve = AsyncMock(side_effect=[SimpleNamespace(items=[item]) for item in memories])
    monkeypatch.setattr(mock_memory_context.MemoryRetrievalService, "retrieve", retrieve)
    started = asyncio.Event()

    async def complete(_db, **kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            started.set()
            await asyncio.Future()
        return ProviderResult(content="Updated answer", finish_reason="stop")

    async def rollback():
        memories[0].__dict__.pop("id", None)
        memories[0]._sa_instance_state.expired_attributes.add("id")

    monkeypatch.setattr(ProviderService, "complete", staticmethod(complete))
    db.rollback.side_effect = rollback
    task = asyncio.create_task(AgentRuntime._execute_loop(db, run))
    AgentRuntime._tasks[run.id] = task
    AgentRuntime._cancellable_runs.add(run.id)
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        reservation = AgentRuntime.reserve_continuation(run.id)
        assert reservation is not None
        AgentRuntime._records.return_value.append(Message(
            id=uuid4(), session_id=run.session_id, role="user", content="Change course"
        ))
        AgentRuntime.finish_continuation(run.id, reservation, accepted=True)
        await asyncio.wait_for(task, timeout=5)
        assert retrieve.await_count == 2
        assert "Change course" in str(requests[-1]["messages"])
    finally:
        _clear_continuation_test_state(run.id)


async def test_direct_message_waits_for_active_tool(monkeypatch) -> None:
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(content="", tool_calls=[_search_call("search-1", "Current")], finish_reason="tool_calls"),
            ProviderResult(content="Updated answer", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def execute(*_args, **_kwargs):
        tool_started.set()
        await release_tool.wait()
        return _search_result()

    tool_registry._execute_definition.side_effect = execute
    db.scalars.return_value = SimpleNamespace(all=lambda: [])
    task = asyncio.create_task(AgentRuntime._execute_loop(db, run))
    AgentRuntime._tasks[run.id] = task
    AgentRuntime._cancellable_runs.add(run.id)
    try:
        await asyncio.wait_for(tool_started.wait(), timeout=5)
        reservation = AgentRuntime.reserve_continuation(run.id)
        assert reservation is not None
        assert run.id not in AgentRuntime._provider_active_runs
        AgentRuntime._records.return_value.append(Message(
            id=uuid4(), session_id=run.session_id, role="user", content="Change after tool"
        ))
        AgentRuntime.finish_continuation(run.id, reservation, accepted=True)
        assert not task.done()
        release_tool.set()
        await asyncio.wait_for(task, timeout=5)
        assert len(requests) == 2
        assert "Change after tool" in str(requests[-1]["messages"])
    finally:
        release_tool.set()
        _clear_continuation_test_state(run.id)


@pytest.mark.parametrize("arrival", ["waiting", "resolving", "creating", "before"])
async def test_direct_message_supersedes_approval_and_replans(monkeypatch, tmp_path, arrival) -> None:
    arguments = {"root": "session", "path": "note.txt", "content": "Updated", "_reason": "Write the note"}
    db, run, requests, _, events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(tool_calls=[
                _search_call("completed", "Current"),
                ToolCall("waiting", "write_file", arguments),
                ToolCall("skipped", "delete_path", {"root": "session", "path": "old.txt"}),
            ], finish_reason="tool_calls"),
            ProviderResult(tool_calls=[ToolCall("replanned", "write_file", arguments)], finish_reason="tool_calls"),
            ProviderResult(content="Updated answer", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    session = db.get.return_value
    records = AgentRuntime._records.return_value
    approvals = []
    created = asyncio.Queue()
    storage = SessionStorage(tmp_path / "sessions")
    session_path = storage.provision(run.session_id)
    target = session_path / "files" / "note.txt"
    for module in (mock_agent_runtime, mock_agent_loop):
        monkeypatch.setattr(module, "RootFileService", lambda session_id, **kwargs: RootFileService(
            session_id, session_storage=storage,
        ))
    monkeypatch.setattr(PermissionPolicy, "evaluate", AsyncMock(side_effect=lambda *_args, **_kwargs: (
        PermissionDecision.ALLOW if approvals and approvals[-1].status == "approved" else PermissionDecision.ASK
    )))
    monkeypatch.setattr(mock_permissions_requests.NotificationService, "create_for_approval", AsyncMock(return_value=None))
    db.scalars.return_value = SimpleNamespace(all=lambda: [])
    db.scalar.side_effect = lambda statement: True if statement.column_descriptions[0]["entity"] is UserAccount else None
    db.get.side_effect = lambda model, key: records[0] if model is Message else session

    def persist(item):
        item.id = item.id or uuid4()
        item.created_at = item.created_at or datetime.now(UTC)
        if isinstance(item, Message):
            records.append(item)
        elif isinstance(item, ApprovalRequest):
            item.status = "pending"
            approvals.append(item)

    db.add.side_effect = persist
    sent = False

    def send_direct_message(*, accepted=True):
        nonlocal sent
        reservation = AgentRuntime.reserve_continuation(run.id)
        assert reservation is not None
        if accepted:
            sent = True
            db.add(Message(
                id=uuid4(), session_id=run.session_id, role="user", content="Change course",
                provider_data={"_matoloop": {"continuationRunId": str(run.id)}},
            ))
        AgentRuntime.finish_continuation(run.id, reservation, accepted=accepted)

    async def flush():
        if arrival == "creating" and len(approvals) == 1 and not sent:
            send_direct_message()

    db.flush.side_effect = flush

    async def publish(_run_id, event):
        events.append(event)
        if event["type"] == "approval.created":
            created.put_nowait(approvals[-1])

    monkeypatch.setattr(run_events, "publish", publish)

    async def execute(call, context, tool):
        if call.id == "completed":
            return _search_result()
        if arrival == "before" and call.id == "waiting":
            send_direct_message()
        return await ToolRegistry._execute_definition(tool_registry, call, context, tool)

    tool_registry._execute_definition.side_effect = execute
    task = asyncio.create_task(AgentRuntime._execute_loop(db, run))
    AgentRuntime._tasks[run.id] = task
    AgentRuntime._cancellable_runs.add(run.id)
    try:
        if arrival != "before":
            old = await asyncio.wait_for(created.get(), timeout=5)
            if arrival in {"waiting", "resolving"}:
                send_direct_message(accepted=False)
                assert run.id not in AgentRuntime._pending_continuations
                assert old.status == "pending"
                assert not task.done()
                if arrival == "resolving":
                    assert AgentRuntime.claim_approval_resolution(old.id)
                send_direct_message()
                if arrival == "resolving":
                    AgentRuntime.release_approval_resolution(old.id)
                    assert not AgentRuntime.resolve_approval(old.id, "allow_once")
            assert not AgentRuntime.claim_approval_resolution(old.id)

        new = await asyncio.wait_for(created.get(), timeout=5)
        assert new.payload["toolCallId"] == "replanned"
        assert not target.exists()
        assert "Change course" in str(requests[-1]["messages"])
        assert SOURCE_ID in str(requests[-1]["messages"])
        assert "tool.superseded" in str(requests[-1]["messages"])
        if arrival != "before":
            assert old.status == "expired"
            assert old.decision is None
            assert old.resolved_at is not None
            assert any(event["type"] == "approval.expired" and event["approvalId"] == str(old.id) for event in events)
        assert AgentRuntime.claim_approval_resolution(new.id)
        new.status = "approved"
        new.decision = "allow_once"
        assert AgentRuntime.resolve_approval(new.id, "allow_once")
        await asyncio.wait_for(task, timeout=5)

        assert target.read_text() == "Updated"
        results = {message.tool_call_id: message for message in _tool_messages(db)}
        assert SOURCE_ID in results["completed"].content
        for call_id in ("waiting", "skipped"):
            assert json.loads(results[call_id].content)["error"]["code"] == "tool.superseded"
        assert run.status == "running"
        assert run.id not in AgentRuntime._waiting_interactions
        assert _assistant_messages(db)[-1].content == "Updated answer"
    except TimeoutError:
        if task.done():
            task.result()
        pytest.fail(f"Approval wait stalled: {[message.content for message in _tool_messages(db)]}")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        _clear_continuation_test_state(run.id)


async def test_redirect_waits_for_active_tool_result_before_cancelling(monkeypatch) -> None:
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="", tool_calls=[_search_call("search-1", "Current")], finish_reason="tool_calls")],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def execute(*_args, **_kwargs):
        tool_started.set()
        await release_tool.wait()
        return _search_result()

    tool_registry._execute_definition.side_effect = execute
    task = asyncio.create_task(AgentRuntime._execute_loop(db, run))
    AgentRuntime._tasks[run.id] = task
    AgentRuntime._cancellable_runs.add(run.id)
    try:
        await asyncio.wait_for(tool_started.wait(), timeout=5)
        assert AgentRuntime.cancel(run.id, redirected=True)
        assert task.cancelling() == 0
        release_tool.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert len(requests) == 1
        assert len(_tool_messages(db)) == 1
        assert SOURCE_ID in _tool_messages(db)[0].content
    finally:
        release_tool.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        _clear_continuation_test_state(run.id)


async def test_direct_message_arriving_at_final_commit_keeps_run_open(monkeypatch) -> None:
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(content="First answer", finish_reason="stop"),
            ProviderResult(content="Revised answer", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    db.scalars.return_value = SimpleNamespace(all=lambda: [])
    accepted = False

    async def commit():
        nonlocal accepted
        if not accepted and _assistant_messages(db):
            accepted = True
            reservation = AgentRuntime.reserve_continuation(run.id)
            assert reservation is not None
            AgentRuntime._records.return_value.append(Message(
                id=uuid4(), session_id=run.session_id, role="user", content="Correction"
            ))
            AgentRuntime.finish_continuation(run.id, reservation, accepted=True)

    db.commit.side_effect = commit
    task = asyncio.create_task(AgentRuntime._execute_loop(db, run))
    AgentRuntime._tasks[run.id] = task
    AgentRuntime._cancellable_runs.add(run.id)
    try:
        await asyncio.wait_for(task, timeout=5)
        assert accepted
        assert len(requests) == 2
        assert "Correction" in str(requests[-1]["messages"])
        assert _assistant_messages(db)[0].provider_data["_matoloop"]["continuationSegment"] is True
    finally:
        _clear_continuation_test_state(run.id)


async def test_available_can_answer_without_research(monkeypatch) -> None:
    answer = "Antwort aus stabilem Wissen"
    db, run, requests, _, events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content=answer, finish_reason="stop")],
        search_policy=WebSearchPolicy.AVAILABLE,
    )

    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == 1
    assert requests[0]["tool_choice"] == "auto"
    assert requests[0]["prompt_cache_key"] == str(run.session_id)
    assert any(event.get("delta") == answer for event in events)
    final = _assistant_messages(db)[0]
    assert final.provider_data["_matoloop"]["research"] == {
        "policy": "available",
        "used": False,
        "linkedSources": [],
    }


async def test_required_search_transitions_to_the_shared_streaming_loop(monkeypatch) -> None:
    answer = f"{'A' * 1_100} {SOURCE_CITATION}"
    db, run, requests, projections, events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                content="Ich recherchiere.",
                tool_calls=[_search_call("search-1", "Aktuelle Frage")],
                provider_data={"chat_reasoning_details": [{"text": "Intern"}]},
                finish_reason="tool_calls",
            ),
            ProviderResult(
                content=answer,
                finish_reason="stop",
                provider_data={"_matoloop": {"providerSupplied": True}},
            ),
        ],
    )
    run.agent_profile_id = GENERAL_PROFILE_ID

    await AgentRuntime._execute_loop(db, run)

    assert [request["tool_choice"] for request in requests] == ["required", "auto"]
    assert [tool["name"] for tool in requests[0]["tools"]] == ["web_search"]
    assert {tool["name"] for tool in requests[1]["tools"]} >= {
        "read",
        "web_search",
    }
    assert "Mandatory web research is active" in projections[0]["system_prompt"]
    assert "Mandatory web research is active" not in projections[1]["system_prompt"]
    assert "Web research contract:" in projections[0]["system_prompt"]
    assert "Web research contract:" in projections[1]["system_prompt"]
    persisted_contents = [message.content for message in _assistant_messages(db)]
    assert "Ich recherchiere." not in persisted_contents
    assert answer in persisted_contents
    assert all(event.get("delta") != "Ich recherchiere." for event in events)
    assert any(event.get("delta") == answer for event in events)
    final = next(message for message in _assistant_messages(db) if not message.tool_calls)
    assert final.provider_data["_matoloop"]["runId"] == str(run.id)
    assert final.provider_data["_matoloop"]["agentProfileId"] == str(
        run.agent_profile_id
    )
    assert "providerSupplied" not in final.provider_data["_matoloop"]
    assert final.provider_data["_matoloop"]["research"] == {
        "policy": "required",
        "used": True,
        "linkedSources": [{"title": SOURCE_TITLE, "url": SOURCE_URL}],
    }
    completed = next(
        event
        for event in events
        if event["type"] == "message.completed" and not event["message"]["toolCalls"]
    )
    assert completed["message"]["research"] == final.provider_data["_matoloop"][
        "research"
    ]
    assert completed["message"]["content"] == answer
    assert completed["message"]["truncated"] is False


async def test_empty_response_after_search_is_repaired(monkeypatch) -> None:
    answer = f"Belegte Antwort {SOURCE_CITATION}"
    db, run, requests, projections, events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[_search_call("search-1", "Aktuelle Frage")],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="", finish_reason="stop"),
            ProviderResult(content=answer, finish_reason="stop"),
        ],
    )

    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == 3
    assert "previous model turn returned neither" not in projections[1]["system_prompt"]
    assert "previous model turn returned neither" in projections[2]["system_prompt"]
    final_messages = [message for message in _assistant_messages(db) if not message.tool_calls]
    assert [message.content for message in final_messages] == [answer]
    assert any(event.get("delta") == answer for event in events)


async def test_empty_response_repair_is_locally_bounded(monkeypatch) -> None:
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(content="", finish_reason="stop"),
            ProviderResult(content="Reparierte Antwort", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
    )

    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == 2
    assert _assistant_messages(db)[0].content == "Reparierte Antwort"


@pytest.mark.parametrize(
    ("model_id", "expected_efforts"),
    [
        ("Qwen3.8-27B-GGUF-UD-Q4_K_XL", ["low", "none"]),
        ("local-model", ["low", "low"]),
    ],
)
async def test_empty_response_repair_disables_supported_reasoning(
    monkeypatch, model_id, expected_efforts
) -> None:
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(content="", finish_reason="stop"),
            ProviderResult(content="Reparierte Antwort", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        reasoning_effort="low",
        model_id=model_id,
    )

    await AgentRuntime._execute_loop(db, run)

    assert [request["reasoning_effort"] for request in requests] == expected_efforts


async def test_repeated_empty_response_fails_without_persisting_it(monkeypatch) -> None:
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[_search_call("search-1", "Aktuelle Frage")],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="", finish_reason="stop"),
            ProviderResult(content="", finish_reason="stop"),
        ],
    )

    with pytest.raises(AgentRunError) as exc_info:
        await AgentRuntime._execute_loop(db, run)

    assert exc_info.value.code == "agent.emptyResponse"
    assert len(requests) == 3
    assert not [message for message in _assistant_messages(db) if not message.tool_calls]


async def test_streamed_text_is_persisted_when_final_result_is_empty(monkeypatch) -> None:
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="", finish_reason="stop")],
        search_policy=WebSearchPolicy.AVAILABLE,
        streamed_contents=["Gestreamte Antwort"],
    )

    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == 1
    assert _assistant_messages(db)[0].content == "Gestreamte Antwort"


async def test_empty_output_at_length_limit_is_not_repaired(monkeypatch) -> None:
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="", finish_reason="length")],
        search_policy=WebSearchPolicy.AVAILABLE,
    )

    with pytest.raises(AgentRunError) as exc_info:
        await AgentRuntime._execute_loop(db, run)

    assert exc_info.value.code == "agent.outputLimitReached"
    assert len(requests) == 1


async def test_empty_required_search_satisfies_the_gate(monkeypatch) -> None:
    db, run, requests, _, events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[_search_call("search-empty", "Sehr seltenes Thema")],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                content="Keine aktuellen Treffer gefunden.", finish_reason="stop"
            ),
        ],
    )
    tool_registry._execute_definition.return_value = _search_result(empty=True)

    await AgentRuntime._execute_loop(db, run)

    assert [request["tool_choice"] for request in requests] == ["required", "auto"]
    assert any(
        event.get("delta") == "Keine aktuellen Treffer gefunden." for event in events
    )
    final = next(message for message in _assistant_messages(db) if not message.tool_calls)
    assert final.provider_data["_matoloop"]["research"] == {
        "policy": "required",
        "used": True,
        "linkedSources": [],
    }


async def test_required_search_retries_hidden_after_tool_failure(monkeypatch) -> None:
    db, run, requests, projections, events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                content="Unbelegte Antwort",
                tool_calls=[_search_call("search-failed", "Fehlerhafte Suche")],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                content="Zweiter Versuch",
                tool_calls=[_search_call("search-ok", "Neu formulierte Suche")],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Normale Antwort", finish_reason="stop"),
        ],
    )
    tool_registry._execute_definition.side_effect = [
        ToolResult.error("integration.unavailable"),
        _search_result(),
    ]

    await AgentRuntime._execute_loop(db, run)

    assert [request["tool_choice"] for request in requests] == [
        "required",
        "required",
        "auto",
    ]
    assert "previous attempt did not complete" in projections[1]["system_prompt"]
    persisted_contents = [message.content for message in _assistant_messages(db)]
    assert "Unbelegte Antwort" not in persisted_contents
    assert "Zweiter Versuch" not in persisted_contents
    assert all(
        event.get("delta") not in {"Unbelegte Antwort", "Zweiter Versuch"}
        for event in events
    )


async def test_failed_mutation_allows_an_honest_negative_final_response(monkeypatch) -> None:
    db, run, _requests, _projections, events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        "write-failed",
                        "write_file",
                        {
                            "root": "session",
                            "path": "note.txt",
                            "content": "Hallo",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Die Datei konnte nicht erstellt werden.", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        tool_result=ToolResult.error("tool.invalidArguments", field="path"),
    )

    await AgentRuntime._execute_loop(db, run)

    assert _tool_messages(db)[0].provider_data["_matoloop"]["errorCode"] == "tool.invalidArguments"
    assert _assistant_messages(db)[-1].content == "Die Datei konnte nicht erstellt werden."
    assert [event.get("delta") for event in events].count("Die Datei konnte nicht erstellt werden.") == 1


@pytest.mark.parametrize("with_question", [False, True])
async def test_response_streams_and_persists_after_failed_webfetch(monkeypatch, with_question) -> None:
    answer = "Die Website war nicht erreichbar."
    response = ProviderResult(content=answer, finish_reason="stop")
    if with_question:
        response.tool_calls = [ToolCall("ask", "question", {"questions": [{
            "id": "next", "type": "free_text", "header": "Quelle",
            "question": "Welche Quelle soll ich stattdessen nutzen?",
        }]})]
        response.finish_reason = "tool_calls"
    db, run, _requests, _projections, events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        "fetch-failed",
                        "webfetch",
                        {"url": "https://example.test/missing"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            response,
            *([ProviderResult(content="Verstanden.", finish_reason="stop")] if with_question else []),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    tool_registry._execute_definition.side_effect = [
        ToolResult.error("webfetch.requestFailed", params={"statusCode": 404}),
        ToolResult.json({"next": ["Eine andere Quelle"]}),
    ]
    complete = ProviderService.complete

    async def assert_streaming(_db, **kwargs):
        result = await complete(_db, **kwargs)
        if result.content == answer:
            assert any(event.get("delta") == answer for event in events)
        return result

    monkeypatch.setattr(ProviderService, "complete", staticmethod(assert_streaming))

    await AgentRuntime._execute_loop(db, run)

    assert _assistant_messages(db)[1].content == answer
    assert [event.get("delta") for event in events].count(answer) == 1


async def test_user_denial_skips_the_remaining_tool_batch(monkeypatch) -> None:
    db, run, _requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        "write-denied",
                        "write_file",
                        {"root": "session", "path": "note.txt", "content": "Hallo"},
                    ),
                    ToolCall(
                        "delete-skipped",
                        "delete_path",
                        {"root": "session", "path": "old.txt", "recursive": False},
                    ),
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Die Aktion wurde abgelehnt.", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    monkeypatch.setattr(
        PermissionPolicy, "evaluate", AsyncMock(return_value=PermissionDecision.ASK)
    )
    request_approval = AsyncMock(return_value=ApprovalResolution(decision="deny"))
    monkeypatch.setattr(
        AgentRuntime,
        "_request_approval",
        request_approval,
    )

    async def execute(_call, context, _tool):
        request = PermissionRequest(
            capability="filesystem.write",
            resource_type="path",
            resource_scope="note.txt",
            action_kind="write_file",
            risk_level="medium",
            title="Datei schreiben?",
            summary="Datei schreiben",
        )
        decision = await context.ask(request)
        assert decision == PermissionDecision.USER_DENIED
        assert await context.ask(request) == PermissionDecision.USER_DENIED
        return ToolResult.error("permission.userDenied", field="requests.1")

    tool_registry._execute_definition.side_effect = execute

    await AgentRuntime._execute_loop(db, run)

    assert tool_registry._execute_definition.await_count == 1
    request_approval.assert_awaited_once()
    assert [message.tool_call_id for message in _tool_messages(db)] == [
        "write-denied",
        "delete-skipped",
    ]
    assert [json.loads(message.content)["error"]["code"] for message in _tool_messages(db)] == [
        "permission.userDenied",
        "tool.skippedAfterUserDenial",
    ]
    assert json.loads(_tool_messages(db)[0].content)["error"]["field"] == "requests.1"
    assert _assistant_messages(db)[-1].content == "Die Aktion wurde abgelehnt."


async def test_user_denial_resolves_required_search(monkeypatch) -> None:
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[_search_call("search-denied", "Aktuelle Frage")],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Die Suche wurde abgelehnt.", finish_reason="stop"),
        ],
    )
    monkeypatch.setattr(
        PermissionPolicy, "evaluate", AsyncMock(return_value=PermissionDecision.ASK)
    )
    monkeypatch.setattr(
        AgentRuntime,
        "_request_approval",
        AsyncMock(return_value=ApprovalResolution(decision="deny")),
    )

    async def execute(_call, context, _tool):
        await context.ask(
            PermissionRequest(
                capability="network.search",
                resource_type="network_service",
                resource_scope="configured_search",
                action_kind="web_search",
                risk_level="medium",
                title="Websuche?",
                summary="Web durchsuchen",
            )
        )
        return ToolResult.error("permission.denied")

    tool_registry._execute_definition.side_effect = execute

    await AgentRuntime._execute_loop(db, run)

    assert [request["tool_choice"] for request in requests] == ["required", "auto"]
    assert json.loads(_tool_messages(db)[0].content)["error"]["code"] == (
        "permission.userDenied"
    )
    assert _assistant_messages(db)[-1].content == "Die Suche wurde abgelehnt."


async def test_successful_mutation_correction_releases_the_final_response(monkeypatch) -> None:
    db, run, _requests, _projections, events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        "write-failed",
                        "write_file",
                        {
                            "root": "session",
                            "path": "wrong.txt",
                            "content": "Hallo",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                content="Schon erledigt.",
                tool_calls=[
                    ToolCall(
                        "write-corrected",
                        "write_file",
                        {
                            "root": "session",
                            "path": "note.txt",
                            "content": "Hallo",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Die Datei wurde erstellt.", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    tool_registry._execute_definition.side_effect = [
        ToolResult.error("tool.invalidArguments", field="path"),
        ToolResult.json({"written": "note.txt"}),
    ]

    await AgentRuntime._execute_loop(db, run)

    persisted = [message.content for message in _assistant_messages(db)]
    assert persisted[-1] == "Die Datei wurde erstellt."
    assert [event.get("delta") for event in events].count("Die Datei wurde erstellt.") == 1


async def test_repeated_failed_tool_call_gets_one_correction_turn(monkeypatch) -> None:
    failed = {"url": "https://example.test/missing"}
    corrected = {"url": "https://example.test/current"}
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[ToolCall("fetch-failed", "webfetch", failed)],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[ToolCall("fetch-repeated", "webfetch", failed)],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[ToolCall("fetch-corrected", "webfetch", corrected)],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Korrigierte Antwort", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    tool_registry._execute_definition.side_effect = [
        ToolResult.error("webfetch.requestFailed", params={"statusCode": 404}),
        ToolResult.json({"content": "Aktuelle Quelle"}),
    ]

    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == 4
    assert [
        call.args[0].arguments
        for call in tool_registry._execute_definition.await_args_list
    ] == [failed, corrected]
    assert [
        json.loads(message.content).get("error", {}).get("code")
        for message in _tool_messages(db)
    ] == ["webfetch.requestFailed", "agent.repeatedToolCall", None]
    assert _assistant_messages(db)[-1].content == "Korrigierte Antwort"


async def test_second_repeated_failed_tool_call_ends_the_run(monkeypatch) -> None:
    arguments = {"url": "https://example.test/missing"}
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[ToolCall("fetch-failed", "webfetch", arguments)],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[ToolCall("fetch-repeated", "webfetch", arguments)],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[ToolCall("fetch-repeated-again", "webfetch", arguments)],
                finish_reason="tool_calls",
            ),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    tool_registry._execute_definition.return_value = ToolResult.error(
        "webfetch.requestFailed", params={"statusCode": 404}
    )

    with pytest.raises(AgentRunError) as exc_info:
        await AgentRuntime._execute_loop(db, run)

    assert exc_info.value.code == "agent.repeatedToolCall"
    assert len(requests) == 3
    tool_registry._execute_definition.assert_awaited_once()


async def test_directory_change_allows_retrying_a_failed_workspace_call(monkeypatch) -> None:
    workspace_read = {"root": "workspace", "path": "README.md"}
    db, run, _requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[ToolCall("read-failed", "read", workspace_read)],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        "change-directory",
                        "change_directory",
                        {"path": "/workspace/drastic-backup"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[ToolCall("read-retried", "read", workspace_read)],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Repository gelesen.", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        execution_agent_id=uuid4(),
        execution_operations=("filesystem.resolve",),
    )
    async def execute(call, _context, _tool):
        if call.name == "change_directory":
            run.current_directory_snapshot = "/workspace/drastic-backup"
            return ToolResult.json({"currentDirectory": run.current_directory_snapshot})
        return ToolResult("README", {}) if run.current_directory_snapshot else ToolResult.error("file.notFound")

    tool_registry._execute_definition.side_effect = execute

    await AgentRuntime._execute_loop(db, run)

    assert [
        call.args[0].name for call in tool_registry._execute_definition.await_args_list
    ] == ["read", "change_directory", "read"]
    assert _assistant_messages(db)[-1].content == "Repository gelesen."


async def test_research_uses_only_the_shared_tool_budget(monkeypatch) -> None:
    db, run, requests, projections, _ = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[_search_call("search-1", "Aktuelle Frage")],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[_search_call("search-2", "Weitere Frage")],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Budgetierte Antwort", finish_reason="stop"),
        ],
        max_tool_calls=25,
        search_policy=WebSearchPolicy.AVAILABLE,
    )

    await AgentRuntime._execute_loop(db, run)

    assert tool_registry._execute_definition.await_count == 2
    assert requests[0]["tools"] == requests[1]["tools"] == requests[2]["tools"]
    assert "Research budget limit" not in projections[1]["system_prompt"]
    tool_messages = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message) and call.args[0].role == "tool"
    ]
    assert len(tool_messages) == 2
    assert all("errorCode" not in message.provider_data["_matoloop"] for message in tool_messages)


async def test_repeated_successful_research_call_does_not_hit_the_network(monkeypatch) -> None:
    db, run, requests, projections, _ = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[_search_call("search-1", "Aktuelle Frage")],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[_search_call("search-2", "Aktuelle Frage")],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Antwort nach Duplikat", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
    )

    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == 3
    assert tool_registry._execute_definition.await_count == 1
    tool_contents = [
        call.args[0].content
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message) and call.args[0].role == "tool"
    ]
    assert any('"alreadyCompleted": true' in content for content in tool_contents)
    repeated = next(
        json.loads(content)
        for content in tool_contents
        if '"alreadyCompleted": true' in content
    )
    original = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message)
        and call.args[0].role == "tool"
        and '"alreadyCompleted": true' not in call.args[0].content
    )
    assert repeated["toolResultReference"] == {
        "tool": "session_tool_result_read",
        "arguments": {"message_id": str(original.id), "offset": 0},
        "totalCharacters": len(original.content),
    }
    assert requests[0]["tools"] == requests[1]["tools"] == requests[2]["tools"]
    assert projections[0]["system_prompt"] == projections[1]["system_prompt"]
    assert projections[1]["system_prompt"] == projections[2]["system_prompt"]
    assert "Research budget limit" not in projections[2]["system_prompt"]


async def test_tool_call_limit_applies_after_required_gate(monkeypatch) -> None:
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[_search_call("search-1", "Aktuelle Frage")],
                finish_reason="tool_calls",
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="read-1",
                        name="read",
                        arguments={"root": "session", "path": "info.txt"},
                    )
                ],
                finish_reason="tool_calls",
            ),
        ],
        max_tool_calls=1,
    )

    with pytest.raises(AgentRunError) as exc_info:
        await AgentRuntime._execute_loop(db, run)

    assert exc_info.value.code == "agent.toolCallLimitReached"
    assert len(requests) == 2
    assert requests[1]["tools"] == []
    assert tool_registry._execute_definition.await_count == 1


async def test_exact_tool_call_limit_allows_tool_free_final_response(monkeypatch) -> None:
    answer = f"Abschluss aus bestätigten Ergebnissen {SOURCE_CITATION}"
    db, run, requests, projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[_search_call("search-1", "Aktuelle Frage")],
                finish_reason="tool_calls",
            ),
            ProviderResult(content=answer, finish_reason="stop"),
        ],
        max_tool_calls=1,
        search_policy=WebSearchPolicy.AVAILABLE,
    )

    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == 2
    assert requests[1]["tools"] == []
    assert "Tool budget: 1 calls" in projections[0][
        "system_prompt"
    ]
    assert "Tool budget exhausted. No tools are available." in projections[1][
        "system_prompt"
    ]
    assert _assistant_messages(db)[-1].content == answer


async def test_oversized_tool_batch_executes_nothing(monkeypatch) -> None:
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    _search_call("search-1", "Erste Frage"),
                    _search_call("search-2", "Zweite Frage"),
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Das Budget reicht nicht fuer diesen Auftrag.", finish_reason="stop"),
        ],
        max_tool_calls=1,
        search_policy=WebSearchPolicy.AVAILABLE,
    )

    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == 2
    assert requests[-1]["tools"] == [] and requests[-1]["tool_choice"] == "none"
    assert len(_tool_messages(db)) == 2
    assert all(json.loads(message.content)["error"]["code"] == "agent.toolCallLimitReached" for message in _tool_messages(db))
    assert _assistant_messages(db)[-1].provider_data["_matoloop"]["completionCause"] == "agent.toolCallLimitReached"
    tool_registry._execute_definition.assert_not_awaited()


async def test_required_search_stops_after_two_semantic_retries(monkeypatch) -> None:
    db, run, requests, _, events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(content="Antwort 1", finish_reason="stop"),
            ProviderResult(content="Antwort 2", finish_reason="stop"),
            ProviderResult(content="Antwort 3", finish_reason="stop"),
        ],
    )

    with pytest.raises(AgentRunError) as exc_info:
        await AgentRuntime._execute_loop(db, run)

    assert exc_info.value.code == "agent.requiredWebSearchMissing"
    assert len(requests) == 3
    assert db.add.call_count == 0
    assert all(event["type"] != "message.delta" for event in events)


@pytest.mark.parametrize(
    ("active_supports_image_input", "expected_model", "private_active"),
    [(True, "local-model", False), (False, "vision-model", False), (False, None, True)],
)
async def test_pdf_vision_prefers_active_model_then_configured_fallback(
    monkeypatch, active_supports_image_input: bool, expected_model: str | None, private_active: bool,
) -> None:
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="fetch-pdf",
                        name="webfetch",
                        arguments={"url": "https://example.test/scan.pdf"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            *([ProviderResult(content='[{"page":1,"text":"Scan text"}]')] if expected_model else []),
            ProviderResult(content="Fertige Antwort", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        active_supports_image_input=active_supports_image_input,
        vision_fallback=True,
    )
    if private_active:
        ai = await SettingsStore.load(db, AiProviderSettings)
        ai.models[0].trust_level = TrustLevel.PRIVATE

    async def execute(_call, context, _tool):
        if expected_model is None:
            assert context.pdf_vision_handler is None
            return ToolResult.error("webfetch.pdfVisionUnavailable")
        assert context.pdf_vision_handler is not None
        extracted = await context.pdf_vision_handler([(1, b"jpeg")], False)
        return ToolResult.json({"content": extracted[1]})

    tool_registry._execute_definition.side_effect = execute

    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == (3 if expected_model else 2)
    if expected_model is None:
        assert not any(request.get("image_inputs") for request in requests)
        return
    assert requests[1]["model"].model_id == expected_model
    assert requests[1]["tool_choice"] == "none"
    assert requests[1]["image_inputs"][0].message_index == 1
    assert requests[1]["image_inputs"][0].data == b"jpeg"


async def test_private_pdf_pages_use_public_active_vision_model(monkeypatch) -> None:
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="fetch-pdf",
                        name="webfetch",
                        arguments={"url": "http://private.test/scan.pdf"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(content='[{"page":1,"text":"Private scan text"}]'),
            ProviderResult(content="Fertige Antwort", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        active_supports_image_input=True,
        vision_fallback=True,
        vision_fallback_private=True,
    )

    async def execute(_call, context, _tool):
        assert context.pdf_vision_handler is not None
        extracted = await context.pdf_vision_handler([(1, b"jpeg")], True)
        return ToolResult.json({"content": extracted[1]})

    tool_registry._execute_definition.side_effect = execute

    await AgentRuntime._execute_loop(db, run)

    assert requests[1]["model"].model_id == "local-model"
    assert requests[1]["target_trust_level"] == TrustLevel.PUBLIC
    assert requests[1]["image_inputs"][0].trust_level == TrustLevel.PRIVATE.value


@pytest.mark.parametrize("kind", ["video", "audio"])
async def test_native_media_uses_loaded_runtime_safety(monkeypatch, kind) -> None:
    monkeypatch.setattr(
        video_analysis, "probe_video",
        lambda _data: MediaInfo("mp4", "video/mp4", 8_000, 640, 360, 0, None),
    )
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(tool_calls=[ToolCall(
                id="media", name=f"analyze_{kind}",
                arguments={"root": "session", "path": "video.mp4"},
            )]),
            ProviderResult(content="Video description" if kind == "video" else
                           '{"segments":[{"startMs":0,"endMs":10,"text":"Quiet audio"}]}'),
            ProviderResult(content="Analysis complete"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE, safety_reserve_tokens=128,
    )
    ai = await SettingsStore.load(db, AiProviderSettings)
    ai.models[0].input_modalities = [ModelInputModality.TEXT, ModelInputModality(kind)]

    async def execute(_call, context, _tool):
        if kind == "audio":
            buffer = BytesIO()
            with wave.open(buffer, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8_000)
                audio.writeframes(b"\x00\x00" * 80)
            return ToolResult.json(await context.audio_analysis_handler(
                buffer.getvalue(), "audio/wav", "audio.wav", TrustLevel.PUBLIC,
                None, "overview", "Describe audible content",
            ))
        return ToolResult.json(await context.video_analysis_handler(
            b"video", TrustLevel.PUBLIC, None, "overview", None,
        ))

    tool_registry._execute_definition.side_effect = execute
    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == 3
    assert requests[1]["media_inputs"][0].modality == kind
    assert requests[1]["safety_reserve_tokens"] == 128


async def test_video_frames_are_analyzed_in_provider_sized_batches(monkeypatch) -> None:
    frames = [VideoFrame(index * 1_000, f"frame-{index}".encode()) for index in range(8)]
    monkeypatch.setattr(
        video_analysis,
        "probe_video",
        lambda _data: MediaInfo("mp4", "video/mp4", 8_000, 640, 360, 0, None),
    )
    monkeypatch.setattr(
        video_analysis, "extract_video_frames", lambda _data, **_kwargs: frames
    )
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="analyze-video",
                        name="analyze_video",
                        arguments={"root": "session", "path": "attachments/video.mp4"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Frames 0 bis 3"),
            ProviderResult(content="Frames 4 bis 7"),
            ProviderResult(content="Fertige Anleitung", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        active_supports_image_input=True,
        safety_reserve_tokens=128,
    )

    async def execute(_call, context, _tool):
        assert context.video_analysis_handler is not None
        result = await context.video_analysis_handler(
            b"video", TrustLevel.PUBLIC, None, "overview", None
        )
        assert [
            (segment["startMs"], segment["endMs"])
            for segment in result["visualSegments"]
        ] == [(0, 3_500), (3_500, 8_000)]
        return ToolResult.json(result)

    tool_registry._execute_definition.side_effect = execute

    await AgentRuntime._execute_loop(db, run)

    image_requests = [request for request in requests if request.get("image_inputs")]
    assert [len(request["image_inputs"]) for request in image_requests] == [4, 4]
    assert all(
        {image.message_index for image in request["image_inputs"]} == {1}
        for request in image_requests
    )
    assert all(
        len([message for message in request["messages"] if message["role"] == "user"])
        == 1
        for request in image_requests
    )
    assert all(request["max_output_tokens"] == 1_024 for request in image_requests)
    assert all(request["safety_reserve_tokens"] == 128 for request in image_requests)
    assert all(
        "at most 300 words" in request["messages"][0]["content"]
        for request in image_requests
    )
    assert [
        image.data for request in image_requests for image in request["image_inputs"]
    ] == [frame.data for frame in frames]


async def test_cancel_does_not_capture_required_gate_text(monkeypatch) -> None:
    db, run, _, _, events = await _runtime_setup(monkeypatch, [])
    visible_response = _VisibleResponse()

    async def cancel_during_gate(_db, **kwargs):
        await kwargs["on_delta"]("Nicht freigegebener Entwurf")
        raise asyncio.CancelledError

    monkeypatch.setattr(
        ProviderService,
        "complete",
        staticmethod(cancel_during_gate),
    )

    with pytest.raises(asyncio.CancelledError):
        await AgentRuntime._execute_loop(db, run, visible_response)

    assert visible_response.content == ""
    assert all(event["type"] != "message.delta" for event in events)
