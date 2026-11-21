import json
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from runtime_helpers import _runtime_setup, _tool_messages

from mato_loop_backend.agent.projection import message_dict
from mato_loop_backend.agent.run_contracts import AgentRunError
from mato_loop_backend.agent.run_support import _safe_tool_arguments, _tool_call_signature
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.agent.tool_repairs import repairable_tool_error, tool_repair_key
from mato_loop_backend.models import AgentRun, Message, WebSearchPolicy
from mato_loop_backend.prompt_eval import PromptCase, _validate_tool_calls
from mato_loop_backend.providers.base import ProviderResult, ToolCall
from mato_loop_backend.sessions.serialization import message_payload
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.tools.contracts import ToolResult
from mato_loop_backend.tools.executor import _omitted_file_argument
from mato_loop_backend.tools.registry import ToolRegistry

pytestmark = pytest.mark.usefixtures("skill_library")


def read_call(identifier, path="note.txt", **extra):
    return ToolCall(identifier, "read", {"root": "files", "path": path, **extra})


@pytest.mark.parametrize("extra", [{"_reason": None}, {"_reason": ""}, {"_reason": " "}, {"_reason": "x" * 501}])
async def test_reason_repair_through_runtime_and_real_registry(monkeypatch, extra):
    initial = read_call("initial", **extra)
    repaired = read_call("repaired", _reason="Die Datei wird für die Antwort benötigt.")
    db, run, requests, _, events = await _runtime_setup(
        monkeypatch,
        [ProviderResult(tool_calls=[initial]), ProviderResult(tool_calls=[repaired]), ProviderResult(content="Gelesen.")],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    handler = AsyncMock(return_value=ToolResult.json({"content": "note"}))

    async def flush():
        for added in db.add.call_args_list:
            if isinstance(added.args[0], Message) and added.args[0].id is None:
                added.args[0].id = uuid4()

    db.flush.side_effect = flush

    async def execute(call, context, tool):
        # Keep the real registry validation; replace only the filesystem handler.
        tool = replace(tool, definition=replace(tool.definition, handler=handler))
        return await ToolRegistry._execute_definition(tool_registry, call, context, tool)

    monkeypatch.setattr(tool_registry, "_execute_definition", execute)
    await AgentRuntime._execute_loop(db, run)

    first, second = _tool_messages(db)
    assert message_payload(first)["argumentsRejected"] is True
    assert message_payload(first)["toolRepair"] == "recovered"
    assert {"type": "message.updated", "messageId": str(first.id)} in events
    assert message_dict(first) == message_dict(Message(role="tool", content=first.content, tool_call_id=first.tool_call_id))
    assert json.loads(first.content)["error"] == {
        "code": "tool.invalidArguments",
        "message": "The tool arguments are invalid. Inspect error.field and error.params.reason and retry with arguments that match the tool schema.",
        "field": "_reason",
        "params": {"reason": "invalid_value", "hint": "Use a non-blank string of at most 500 characters, or omit _reason."},
    }
    assert json.loads(second.content) == {"content": "note"}
    assert len(requests) == 3
    handler.assert_awaited_once()


async def test_missing_reason_executes_without_a_repair_round(monkeypatch):
    call = read_call("initial")
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch, [ProviderResult(tool_calls=[call]), ProviderResult(content="Gelesen.")],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    handler = AsyncMock(return_value=ToolResult.json({"content": "note"}))

    async def execute(call, context, tool):
        tool = replace(tool, definition=replace(tool.definition, handler=handler))
        return await ToolRegistry._execute_definition(tool_registry, call, context, tool)

    monkeypatch.setattr(tool_registry, "_execute_definition", execute)
    await AgentRuntime._execute_loop(db, run)
    assert len(requests) == 2
    assert json.loads(_tool_messages(db)[0].content) == {"content": "note"}
    handler.assert_awaited_once()


@pytest.mark.parametrize("repair", [True, False])
async def test_repeated_batch_gets_one_model_round_to_repair(monkeypatch, repair):
    calls = [read_call(str(i), path=f"{i}.txt", _reason=" ") for i in range(2)]
    final_calls = [read_call(f"fixed-{i}", path=f"{i}.txt", _reason="Die Datei wird benötigt.") for i in range(2)] if repair else calls
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch,
        [ProviderResult(tool_calls=calls), ProviderResult(tool_calls=calls), ProviderResult(tool_calls=final_calls), ProviderResult(content="Fertig.")],
        search_policy=WebSearchPolicy.AVAILABLE,
    )

    handler = AsyncMock(return_value=ToolResult.json({"content": "note"}))

    async def execute(call, context, tool):
        tool = replace(tool, definition=replace(tool.definition, handler=handler))
        return await ToolRegistry._execute_definition(tool_registry, call, context, tool)

    tool_registry._execute_definition.side_effect = execute
    if repair:
        await AgentRuntime._execute_loop(db, run)
        assert len(requests) == 4
        assert tool_registry._execute_definition.await_count == 4
    else:
        with pytest.raises(AgentRunError, match="agent.repeatedToolCall"):
            await AgentRuntime._execute_loop(db, run)
        assert len(requests) == 3
        assert tool_registry._execute_definition.await_count == 2

    assert [message_payload(message).get("argumentsRejected", False) for message in _tool_messages(db)
            if message.provider_data["_matoloop"].get("errorCode") == "agent.repeatedToolCall"] == (
        [True, True] if repair else [True, True, False, False]
    )


async def test_changing_reason_does_not_retry_execution_failure(monkeypatch):
    calls = [read_call(str(i), _reason=f"Begründung {i}") for i in range(3)]
    db, run, _, _, _ = await _runtime_setup(
        monkeypatch, [ProviderResult(tool_calls=[call]) for call in calls],
        search_policy=WebSearchPolicy.AVAILABLE, tool_result=ToolResult.error("file.notFound"),
    )
    with pytest.raises(AgentRunError, match="agent.repeatedToolCall"):
        await AgentRuntime._execute_loop(db, run)
    tool_registry._execute_definition.assert_awaited_once()
    assert all(not message_payload(message).get("argumentsRejected") for message in _tool_messages(db))


def test_reason_survives_persisted_history_projection():
    arguments = {"root": "files", "path": "note.txt", "_reason": "Den Inhalt für die Antwort prüfen."}
    message = Message(role="assistant", content="", tool_calls=[{
        "id": "read", "name": "read", "arguments": _safe_tool_arguments("read", arguments),
    }])
    assert message_dict(message)["tool_calls"][0]["arguments"] == arguments


@pytest.mark.parametrize(
    "tool,first,second",
    [
        (
            "edit_file",
            {"path": "note.txt", "oldString": "[Persisted edit text omitted: 11 characters.]", "newString": "new"},
            {"path": "note.txt", "oldString": "[Persisted edit text omitted: 999 characters.]", "newString": "new"},
        ),
        (
            "apply_patch",
            {"patchText": {"characters": 11, "redacted": True}},
            {"patchText": {"characters": 999, "redacted": True}},
        ),
    ],
)
def test_omitted_historical_file_content_has_stable_repeat_signature(tool, first, second):
    assert _tool_call_signature(
        ToolCall("first", tool, first), include_reason=True
    ) == _tool_call_signature(
        ToolCall("second", tool, second), include_reason=True
    )


def test_omitted_historical_patch_is_rejected_before_schema_validation():
    assert _omitted_file_argument(
        "apply_patch", {"patchText": {"characters": 11, "redacted": True}}
    ) == "patchText"


async def test_successful_write_does_not_reset_omitted_patch_failure(monkeypatch):
    first = ToolCall("patch-1", "apply_patch", {
        "patchText": {"characters": 11, "redacted": True},
    })
    write = ToolCall("write", "write_file", {
        "root": "session", "path": "note.txt", "content": "fresh",
    })
    repeated = ToolCall("patch-2", "apply_patch", {
        "patchText": {"characters": 999, "redacted": True},
    })
    db, run, *_ = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(tool_calls=[first]),
            ProviderResult(tool_calls=[write]),
            ProviderResult(tool_calls=[repeated]),
            ProviderResult(content="Done."),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        execution_agent_id=uuid4(),
        execution_operations=("filesystem.patch",),
    )
    run.current_directory_snapshot = "/work"
    tool_registry._execute_definition.side_effect = [
        ToolResult.error("file.omittedContent", field="patchText"),
        ToolResult.json({"written": "note.txt"}),
    ]

    await AgentRuntime._execute_loop(db, run)

    assert [call.args[0].name for call in tool_registry._execute_definition.await_args_list] == [
        "apply_patch", "write_file",
    ]
    assert _tool_messages(db)[-1].provider_data["_matoloop"]["errorCode"] == "agent.repeatedToolCall"


async def test_instruction_deferral_does_not_resolve_failed_edit(monkeypatch):
    calls = [ToolCall(str(i), "edit_file", {"path": "note.txt", "oldString": str(i), "newString": "fixed"}) for i in range(2)]
    db, run, _, _, _ = await _runtime_setup(
        monkeypatch, [*[ProviderResult(tool_calls=[call]) for call in calls], ProviderResult(content="Deferred.")],
        search_policy=WebSearchPolicy.AVAILABLE,
    )
    tool_registry._execute_definition.side_effect = [
        ToolResult.error("file.editTextNotFound"), ToolResult("Not executed.", {"repositoryInstructionsDeferred": True}),
    ]
    await AgentRuntime._execute_loop(db, run)
    assert message_payload(_tool_messages(db)[0])["toolRepair"] == "pending"


async def test_unrelated_copy_does_not_resolve_argument_failure(monkeypatch):
    calls = [
        ToolCall("failed", "copy_path", {"source_root": "files", "source_path": "a.txt", "destination_path": "b.txt"}),
        ToolCall("unrelated", "copy_path", {"source_root": "files", "source_path": "c.txt", "destination_root": "files", "destination_path": "d.txt"}),
    ]
    db, run, *_ = await _runtime_setup(
        monkeypatch, [*[ProviderResult(tool_calls=[call]) for call in calls], ProviderResult(content="Done.")],
        search_policy=WebSearchPolicy.DISABLED,
    )
    rejected = ToolResult.error("tool.invalidArguments")
    rejected.metadata["argumentsRejected"] = True
    tool_registry._execute_definition.side_effect = [rejected, ToolResult.json({"copied": "d.txt"})]
    await AgentRuntime._execute_loop(db, run)
    assert message_payload(_tool_messages(db)[0])["toolRepair"] == "pending"


@pytest.mark.parametrize("directory", ["/work", "C:/work"])
def test_repair_target_matches_relative_and_absolute_file_paths(directory):
    run = AgentRun(execution_agent_id=uuid4(), current_directory_snapshot=directory)
    assert tool_repair_key(ToolCall("a", "edit_file", {"path": "note.txt"}), run) == tool_repair_key(
        ToolCall("b", "edit_file", {"root": "computer", "path": directory + "/note.txt"}), run,
    )


@pytest.mark.parametrize("code", ["tool.executionFailed", "file.patchIncomplete", "browser.outcomeUnknown", "permission.denied"])
def test_uncertain_effects_and_denials_are_not_hidden_by_workflow_repairs(code):
    assert not repairable_tool_error(ToolCall("call", "memory_write", {}), {"errorCode": code}, workflow=True)


@pytest.mark.parametrize("extra,valid", [({}, True), ({"_reason": None}, False), ({"_reason": " "}, False), ({"_reason": "Prüfen."}, True)])
def test_prompt_evaluator_accepts_missing_reason_and_validates_supplied_value(extra, valid):
    case = PromptCase(id="reason-contract", kind="general", user="Lies note.txt.", tools=("read",))
    assert (not _validate_tool_calls(case, [read_call("read", **extra)])) is valid
