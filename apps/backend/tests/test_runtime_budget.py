from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from runtime_helpers import (
    _assistant_messages,
    _runtime_setup,
    _search_call,
    _tool_messages,
)

import mato_loop_backend.agent.loop as mock_agent_loop
import mato_loop_backend.agent.run_support as mock_agent_run_support
import mato_loop_backend.agent.runtime as mock_agent_runtime
from mato_loop_backend.agent.run_contracts import AgentRunError
from mato_loop_backend.agent.run_support import (
    _has_simulated_tool_call,
    _retryable_transient_read,
    _tool_call_signature,
)
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.agent.subagents import integration_ceiling_ids, permission_snapshot
from mato_loop_backend.files.service import RootFileError
from mato_loop_backend.models import ExecutionAgent, Message, WebSearchPolicy
from mato_loop_backend.providers.base import ProviderResult, ToolCall
from mato_loop_backend.providers.service import ProviderService
from mato_loop_backend.sessions.serialization import message_payload
from mato_loop_backend.settings import IntegrationInstance, IntegrationSettings, ToolSettings
from mato_loop_backend.settings.effective import filter_agent_profile_integrations
from mato_loop_backend.settings.models import AgentProfile, IntegrationPermissions
from mato_loop_backend.tools.arguments import ProtectedQuestionResolution
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.tools.contracts import ToolResult
from mato_loop_backend.tools.registry import ToolRegistry
from mato_loop_backend.tools.secrets import resolve_sudo_password


@pytest.mark.parametrize("transmitted", [30_000, None])
async def test_runtime_passes_projection_reserve_and_records_provider_allocation(monkeypatch, transmitted):
    output_budget = {
        "estimatedInputTokens": 150,
        "finalEstimatedInputTokens": 150,
        "guardedInputTokens": 165,
        "reservedOutputTokens": 2_048,
        "allocatedOutputTokens": 30_000,
        "transmittedMaxOutputTokens": transmitted,
        "safetyReserveTokens": 4_096,
        "manualLimit": None,
        "providerLimit": 32_768,
        "helperLimit": None,
        "mode": "auto",
        "estimationPolicy": "serialized-characters-media-reserves-v1",
        "mediaInputTokensUnknown": False,
    }
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Antwort", provider_data={"output_budget": output_budget})],
        max_tool_calls=7, search_policy=WebSearchPolicy.AVAILABLE,
    )
    await AgentRuntime._execute_loop(db, run)

    prepared = await ProviderService.prepare_request(**{
        key: requests[0][key] for key in (
            "provider", "model", "messages", "tools", "tool_choice", "timeout",
            "target_trust_level", "pseudonymization_enabled", "prompt_cache_key",
            "required_image_ids", "reasoning_effort",
        )
    })
    assert requests[0]["estimated_input_tokens"] == prepared.estimated_input_tokens
    assert requests[0]["reserved_output_tokens"] == 2_048
    assert requests[0]["safety_reserve_tokens"] == 4_096
    assert "max_output_tokens" not in requests[0]
    performance = _assistant_messages(db)[0].provider_data["_matoloop"]["performance"]
    assert {key: performance[key] for key in output_budget} == output_budget
    assert performance["inputBudgetTokens"] == 1_000
    assert performance["maxToolCalls"] == 7


@pytest.mark.parametrize(("limit", "calls"), [(25, 25), (None, 61)])
async def test_profile_budget_snapshot_and_unlimited_run(monkeypatch, limit, calls):
    db, run, requests, projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(tool_calls=[_search_call(str(index), f"Question {index}")]) for index in range(calls)]
        + [ProviderResult(content="Ergebnisse zusammengefasst.")],
        max_tool_calls=limit,
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    profile = AgentProfile(name="Custom", instructions="Follow the task.", maxToolCalls=1)
    _mock_value = AsyncMock(return_value=(ToolSettings(), profile))
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'load_agent_profile_tools', _mock_value)

    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == calls + 1
    assert len({request["pseudonymization_key"] for request in requests}) == 1
    assert len(requests[0]["pseudonymization_key"]) == 32
    assert tool_registry._execute_definition.await_count == calls
    assert len(_tool_messages(db)) == calls
    assert requests[-1]["tools"] == requests[-2]["tools"]
    assert requests[-1]["tool_choice"] == "auto"
    performance = _assistant_messages(db)[-1].provider_data["_matoloop"]["performance"]
    assert performance["toolCallsUsed"] == calls
    assert performance["researchToolCallsUsed"] == calls
    assert performance["modelRequest"] == calls + 1
    assert performance["maxToolCalls"] == limit
    if limit is None:
        assert "Tool budget:" not in projections[-1]["system_prompt"]
    else:
        assert message_payload(_assistant_messages(db)[-1])["warnings"] == ["agent.toolCallLimitReached"]


@pytest.mark.parametrize("reason", ["length", "max_tokens", "max_output_tokens", "MAX_TOKENS"])
async def test_output_truncated_batch_has_no_effects_and_full_batch_can_be_repaired(monkeypatch, reason):
    calls = [ToolCall(str(index), "write_file", {"root": "session", "path": f"{index}.txt", "content": "text"}) for index in range(2)]
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(tool_calls=calls, finish_reason=reason),
         ProviderResult(tool_calls=[ToolCall(f"repair-{call.id}", call.name, call.arguments) for call in calls]),
         ProviderResult(content="Beide Dateien erstellt.")],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    async def execute(call, _context, _tool):
        assert call.id.startswith("repair-")
        assert len(_tool_messages(db)) >= 2
        return ToolResult.json({"written": call.arguments["path"]})
    tool_registry._execute_definition.side_effect = execute

    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == 3
    assert tool_registry._execute_definition.await_count == 2
    tools = _tool_messages(db)
    assert [message.tool_call_id for message in tools] == ["0", "1", "repair-0", "repair-1"]
    assert all(message.provider_data["_matoloop"]["errorCode"] == "agent.outputLimitReached" for message in tools[:2])
    assert all("complete batch" in message.content for message in tools[:2])
    assert [item.args[0].arguments for item in tool_registry._execute_definition.await_args_list] == [call.arguments for call in calls]


async def test_exhausted_budget_preserves_rejected_response_despite_file_conflict(monkeypatch):
    finalize = AgentRuntime._finalize_run_files
    text = "Ich lege den Stack jetzt an.\n\n<tool_call>\n<function=write_file>\n</function>\n</tool_call>"
    db, run, _requests, _projections, _events = await _runtime_setup(
        monkeypatch, [ProviderResult(tool_calls=[_search_call("search", "Deployment")]), ProviderResult(content="")],
        max_tool_calls=1, search_policy=WebSearchPolicy.AVAILABLE, streamed_contents=["", text],
    )
    monkeypatch.setattr(AgentRuntime, "_finalize_run_files", finalize)
    monkeypatch.setattr(mock_agent_runtime.AttachmentService, "finalize_run", AsyncMock(
        side_effect=RootFileError("file.changed", "File changed during publication"),
    ))
    with pytest.raises(AgentRunError, match="agent.toolCallLimitReached"):
        await AgentRuntime._execute_loop(db, run)
    assert _assistant_messages(db)[-1].content == text


@pytest.mark.parametrize("wrapper", ["```xml\n{}\n```", "    {}", "`{}`", "> {}"])
def test_simulated_call_detector_preserves_markdown_examples(wrapper):
    assert not _has_simulated_tool_call(wrapper.format("<tool_call><function=write_file></function></tool_call>"))


async def test_last_budget_slot_does_not_open_secret_input(monkeypatch):
    db, run, _requests, _projections, _events = await _runtime_setup(
        monkeypatch, [ProviderResult(tool_calls=[ToolCall("secret", "secret_input", {"inputs": []})]),
                      ProviderResult(content="Zugangsdaten können erst im nächsten Lauf verwendet werden.")],
        max_tool_calls=1, search_policy=WebSearchPolicy.AVAILABLE,
    )
    await AgentRuntime._execute_loop(db, run)
    tool_registry._execute_definition.assert_not_awaited()


async def test_exhausted_budget_retains_streamed_only_summary(monkeypatch):
    db, run, _requests, _projections, _events = await _runtime_setup(
        monkeypatch, [ProviderResult(tool_calls=[_search_call("search", "Deployment")]), ProviderResult(content="")],
        max_tool_calls=1, search_policy=WebSearchPolicy.AVAILABLE, streamed_contents=["", "Deployment noch offen."],
    )
    await AgentRuntime._execute_loop(db, run)
    assert _assistant_messages(db)[-1].content == "Deployment noch offen."


async def test_only_pre_execution_stale_shell_refusal_can_be_retried_once(monkeypatch):
    call = ToolCall("shell", "shell_run", {"command": "cat README.md"})
    db, run, _requests, _projections, _events = await _runtime_setup(
        monkeypatch, [ProviderResult(tool_calls=[call])] * 3 + [ProviderResult(content="Blockiert.")],
        search_policy=WebSearchPolicy.AVAILABLE, execution_agent_id=uuid4(), execution_operations=("shell.run",),
    )
    run.current_directory_snapshot = "/work"
    refusal = ToolResult.error("approval.targetChanged")
    refusal.metadata["retryAfterFreshApproval"] = True
    tool_registry._execute_definition.return_value = refusal
    await AgentRuntime._execute_loop(db, run)
    assert tool_registry._execute_definition.await_count == 2


async def test_truncated_repairs_are_bounded_even_with_unlimited_budget(monkeypatch):
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(tool_calls=[_search_call(str(index), f"Question {index}")], finish_reason="length") for index in range(3)],
        max_tool_calls=None,
    )
    with pytest.raises(AgentRunError, match="agent.outputLimitReached"):
        await AgentRuntime._execute_loop(db, run)
    assert len(requests) == len(_tool_messages(db)) == 3
    tool_registry._execute_definition.assert_not_awaited()


async def test_independent_runtime_invocations_do_not_share_privacy_keys(monkeypatch):
    keys = []
    for _ in range(2):
        db, run, requests, _projections, _events = await _runtime_setup(
            monkeypatch, [ProviderResult(content="Done")],
            search_policy=WebSearchPolicy.AVAILABLE,
        )
        await AgentRuntime._execute_loop(db, run)
        keys.append(requests[0]["pseudonymization_key"])
        assert all("pseudonymization_key" not in str(item.provider_data) for item in _assistant_messages(db))
    assert keys[0] != keys[1]


async def test_alternative_edit_patch_repair_retains_original_failure(monkeypatch):
    edit = ToolCall("edit", "edit_file", {"root": "workspace", "path": "note.txt", "oldText": "old", "newText": "new"})
    patch = ToolCall("patch", "apply_patch", {"patch": "*** Begin Patch\n*** Update File: note.txt\n@@\n-old\n+new\n*** End Patch"})
    db, run, _requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(tool_calls=[edit]), ProviderResult(tool_calls=[patch]), ProviderResult(content="Die Datei wurde per Patch korrigiert.")],
        search_policy=WebSearchPolicy.AVAILABLE,
        execution_agent_id=uuid4(), execution_operations=("filesystem.patch",),
    )
    run.current_directory_snapshot = "/work"
    tool_registry._execute_definition.side_effect = [ToolResult.error("file.notFound"), ToolResult.json({"written": "note.txt"})]
    await AgentRuntime._execute_loop(db, run)
    assert [call.args[0].name for call in tool_registry._execute_definition.await_args_list] == ["edit_file", "apply_patch"]
    assert _tool_messages(db)[0].provider_data["_matoloop"]["errorCode"] == "file.notFound"
    assert "per Patch" in _assistant_messages(db)[-1].content


async def test_unrelated_success_does_not_reset_failed_call_protection(monkeypatch):
    failed = ToolCall("failed", "write_file", {"root": "session", "path": "missing/note.txt", "content": "text"})
    other = ToolCall("other", "read", {"root": "session", "path": "other.txt"})
    db, run, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(tool_calls=[call]) for call in [failed, other, failed, other, failed]],
        max_tool_calls=None, search_policy=WebSearchPolicy.AVAILABLE,
    )
    tool_registry._execute_definition.side_effect = [ToolResult.error("file.notFound"), ToolResult("text", {}), ToolResult("text", {})]
    with pytest.raises(AgentRunError, match="agent.repeatedToolCall"):
        await AgentRuntime._execute_loop(db, run)
    assert len(requests) == 5
    assert tool_registry._execute_definition.await_count == 3
    assert _tool_messages(db)[-1].provider_data["_matoloop"]["errorCode"] == "agent.repeatedToolCall"


@pytest.mark.parametrize(("matching_agent", "input_inside_call"), [(True, False), (False, False), (True, True)])
async def test_sudo_retry_requires_changed_authentication_for_the_selected_agent(monkeypatch, matching_agent, input_inside_call):
    agent_id = uuid4()
    name = f"SUDO_PASSWORD_{(agent_id if matching_agent else uuid4()).hex.upper()}"
    command = ToolCall("admin", "shell_run", {"command": "id -u", "elevated": True})
    secret = ToolCall("password", "secret_input", {"inputs": [
        {"name": name, "label": "sudo", "question": "Enter the password"},
    ]})
    calls = [command, command] if input_inside_call else [command, secret, command]
    if matching_agent and not input_inside_call:
        calls += [secret, command]
    db, run, _requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(tool_calls=[call]) for call in calls] + [ProviderResult(content="Done")],
        search_policy=WebSearchPolicy.AVAILABLE, execution_agent_id=agent_id,
        execution_operations=("shell.run", "shell.classify", "administrator.run"),
    )
    run.current_directory_snapshot = "/work"
    session = db.get.return_value
    db.get.side_effect = lambda model, *args, **kwargs: (
        ExecutionAgent(id=agent_id, user_id=session.user_id) if model is ExecutionAgent else session
    )
    question = AsyncMock(return_value=ProtectedQuestionResolution({name: ["private password"]}, frozenset()))
    monkeypatch.setattr(AgentRuntime, "_request_question", question)
    passwords = []

    async def execute(call, context, bound_tool):
        if call.name == "secret_input":
            return await ToolRegistry._execute_definition(tool_registry, call, context, bound_tool)
        if input_inside_call and name not in context.run_secret_refs:
            secret_id = uuid4()
            context.one_time_secrets[secret_id] = "private password"
            context.run_secret_refs[name] = f"run-secret:{secret_id}"
        password = await resolve_sudo_password(context, None, context.execution_client.agent_id)
        passwords.append(password)
        return ToolResult.error("process.sudoAuthRequired" if password is None else "process.commandFailed")

    tool_registry._execute_definition.side_effect = execute
    await AgentRuntime._execute_loop(db, run)

    expected_passwords = [None, "private password"] if matching_agent else [None]
    if input_inside_call:
        expected_passwords = ["private password"]
    assert passwords == expected_passwords
    assert _tool_messages(db)[-1].provider_data["_matoloop"]["errorCode"] == "agent.repeatedToolCall"
    assert question.await_count == (0 if input_inside_call else 1)


def test_only_one_identical_snapshot_retry_is_allowed_after_unstable_observation():
    snapshot = ToolCall("snapshot", "browser_snapshot", {"tabId": str(uuid4())})
    signature = _tool_call_signature(snapshot)
    failures = {signature: "browser.observationUnstable"}
    assert _retryable_transient_read(snapshot, signature, failures, set())
    assert not _retryable_transient_read(snapshot, signature, failures, {signature})
    failures[signature] = "browser.actionFailed"
    assert not _retryable_transient_read(snapshot, signature, failures, set())
    mutation = ToolCall("click", "browser_click", snapshot.arguments)
    assert not _retryable_transient_read(mutation, signature, failures, set())


def test_only_one_identical_webfetch_retry_is_allowed_after_empty_response():
    call = ToolCall("fetch", "webfetch", {"url": "https://example.com/data"})
    signature = _tool_call_signature(call)
    failures = {signature: "webfetch.emptyResponse"}

    assert _retryable_transient_read(call, signature, failures, set())
    assert not _retryable_transient_read(call, signature, failures, {signature})


def test_transcript_signature_canonicalizes_youtube_url():
    shared = ToolCall(
        "shared", "fetch_transcript", {"url": "https://youtu.be/6RDAAZBYEzs?si=token"}
    )
    canonical = ToolCall(
        "canonical", "fetch_transcript", {"url": "https://www.youtube.com/watch?v=6RDAAZBYEzs"}
    )

    assert _tool_call_signature(shared) == _tool_call_signature(canonical)


@pytest.mark.parametrize(("old_limit", "new_limit"), [(None, 2), (2, None)])
@pytest.mark.parametrize("legacy", [False, True])
async def test_retry_snapshots_current_budget_without_overwriting_failed_run(monkeypatch, old_limit, new_limit, legacy):
    count = new_limit if new_limit is not None else old_limit + 1
    db, failed, requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(tool_calls=[_search_call(str(index), f"Question {index}")]) for index in range(count)]
        + [ProviderResult(content="Ergebnisse zusammengefasst.")],
        max_tool_calls=old_limit, search_policy=WebSearchPolicy.AVAILABLE,
    )
    session = db.get.return_value
    message = (await AgentRuntime._records(db, session.id))[0]
    if legacy:
        message.provider_data = {"_matoloop": {"maxToolCalls": old_limit}}
    failed.status = "failed"
    failed.model_config_id = session.model_config_id
    profile = AgentProfile(name="Custom", instructions="Follow the task.", maxToolCalls=new_limit)
    failed.agent_profile_id = session.agent_profile_id = profile.id
    _mock_value = AsyncMock(return_value=profile)
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'load_agent_profile', _mock_value)
    _mock_value = AsyncMock(return_value=(ToolSettings(), profile))
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'load_agent_profile_tools', _mock_value)
    monkeypatch.setattr(AgentRuntime, "_schedule_run", Mock())
    db.get.side_effect = lambda model, _key, **_kw: message if model is Message else session

    run = await AgentRuntime.retry(db, session, failed)
    assert message.provider_data["_matoloop"]["maxToolCallsByRun"] == {str(failed.id): old_limit, str(run.id): new_limit}
    profile.max_tool_calls = old_limit
    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == count + 1
    assert tool_registry._execute_definition.await_count == count
    assert requests[-1]["tools"] == requests[-2]["tools"]
    assert _assistant_messages(db)[-1].provider_data["_matoloop"]["performance"]["maxToolCalls"] == new_limit


@pytest.mark.parametrize("repair_succeeds", [False, True])
async def test_shell_retry_after_edit_keeps_failure_evidence(monkeypatch, repair_succeeds):
    command = ToolCall("check", "shell_run", {"command": "pytest"})
    edit = ToolCall("repair", "edit_file", {"root": "computer", "path": "test_app.py", "oldText": "old", "newText": "new"})
    db, run, _requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(tool_calls=[command]), ProviderResult(tool_calls=[edit]), ProviderResult(tool_calls=[command]),
         ProviderResult(content="Die Pruefung ist abgeschlossen.")],
        search_policy=WebSearchPolicy.AVAILABLE, execution_agent_id=uuid4(), execution_operations=("shell.run",),
    )
    run.current_directory_snapshot = "/work"
    failure = ToolResult.json({"exitCode": 1, "output": {"stdout": "FAILED test_app.py"}}, metadata={"errorCode": "process.commandFailed"})
    tool_registry._execute_definition.side_effect = [failure, ToolResult.json({"written": "test_app.py"}) if repair_succeeds else ToolResult.error("file.changed"), ToolResult.json({"exitCode": 0})]
    await AgentRuntime._execute_loop(db, run)
    assert tool_registry._execute_definition.await_count == (3 if repair_succeeds else 2)
    assert _tool_messages(db)[0].provider_data["_matoloop"]["errorCode"] == "process.commandFailed"
    assert "FAILED test_app.py" in _tool_messages(db)[0].content
    assert _tool_messages(db)[-1].provider_data["_matoloop"].get("errorCode") == (None if repair_succeeds else "agent.repeatedToolCall")


@pytest.mark.parametrize("call", [ToolCall("read", "read", {"root": "computer", "path": "README.md"}), ToolCall("check", "shell_run", {"command": "pytest"})])
async def test_cwd_changes_failed_call_preconditions(monkeypatch, call):
    db, run, _requests, _projections, _events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(tool_calls=[call]), ProviderResult(tool_calls=[ToolCall("cd", "change_directory", {"path": "/other"})]), ProviderResult(tool_calls=[call]), ProviderResult(content="Geprueft.")],
        search_policy=WebSearchPolicy.AVAILABLE, execution_agent_id=uuid4(), execution_operations=("shell.run", "filesystem.resolve"),
    )
    run.current_directory_snapshot = "/work"
    async def execute(current, _context, _tool):
        if current.name == "change_directory":
            run.current_directory_snapshot = "/other"
            return ToolResult.json({"currentDirectory": "/other"})
        return ToolResult.error("file.notFound") if run.current_directory_snapshot == "/work" else ToolResult("OK", {})
    tool_registry._execute_definition.side_effect = execute
    await AgentRuntime._execute_loop(db, run)
    assert tool_registry._execute_definition.await_count == 3
    assert _tool_messages(db)[-1].content == "OK"


def test_child_integration_snapshot_is_concrete_and_missing_ids_fail_closed():
    first = IntegrationInstance(name="First", integration_type="search", adapter_type="searxng")
    second = IntegrationInstance(name="Second", integration_type="search", adapter_type="searxng")
    settings = IntegrationSettings(instances=[first, second])
    profile = AgentProfile(name="Custom", instructions="Follow the task.", integrationsEnabledByDefault=False)
    first.profile_permissions[profile.id] = IntegrationPermissions(available=True)
    selected = filter_agent_profile_integrations(settings, profile)
    snapshot = permission_snapshot(ToolSettings(), {"web_search"}, selected)
    assert snapshot["integrationIds"] == [str(first.id)]
    profile.integrations_enabled_by_default = True
    live = IntegrationSettings(instances=[first, second, IntegrationInstance(id=uuid4(), name="New", integration_type="search", adapter_type="searxng")])
    scoped = filter_agent_profile_integrations(live, profile, allowed_ids=integration_ceiling_ids(snapshot))
    assert [item.id for item in scoped.instances] == [first.id]
    del snapshot["integrationIds"]
    assert integration_ceiling_ids(snapshot) == []
    assert integration_ceiling_ids({}) == []
    assert integration_ceiling_ids({"integrationIds": None}) == []
    assert integration_ceiling_ids(None) is None


@pytest.mark.parametrize(("offered_allow", "spawn_allow"), [(False, True), (True, False), (True, True)])
def test_delegation_intersects_shell_prefix_permissions(offered_allow, spawn_allow):
    from mato_loop_backend.agent.subagents import ceiling_settings
    from mato_loop_backend.permissions.policy import PermissionRequest, shell_command_scope
    from mato_loop_backend.settings import ToolExecutionPolicy, TrustLevel

    snapshots = [permission_snapshot(
        ToolSettings(shell_prefixes={"read": [["pytest"]] if allowed else []}).select_trust(TrustLevel.PUBLIC),
        {"shell_run"},
    ) for allowed in (offered_allow, spawn_allow)]
    snapshot = snapshots[1]
    snapshot["offeredCeiling"] = snapshots[0]
    request = PermissionRequest(
        capability="shell.read", resource_type="shell_command",
        resource_scope=shell_command_scope(uuid4(), "/work", "read", [["pytest"]], [], True),
        action_kind="shell_run", risk_level="low", title="Run", summary="Run tests",
    )
    assert mock_agent_run_support._tool_policy(
        ceiling_settings(snapshot, TrustLevel.PUBLIC), IntegrationSettings(), request, snapshot=snapshot,
    ) == (ToolExecutionPolicy.AUTO if offered_allow and spawn_allow else ToolExecutionPolicy.ASK)
