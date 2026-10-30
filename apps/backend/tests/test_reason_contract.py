import json
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from runtime_helpers import _runtime_setup, _tool_messages

from mato_loop_backend.agent.projection import message_dict
from mato_loop_backend.agent.run_contracts import AgentRunError
from mato_loop_backend.agent.run_support import _safe_tool_arguments
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.models import Message, WebSearchPolicy
from mato_loop_backend.prompt_eval import PromptCase, _validate_tool_calls
from mato_loop_backend.providers.base import ProviderResult, ToolCall
from mato_loop_backend.sessions.serialization import message_payload
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.tools.contracts import ToolResult
from mato_loop_backend.tools.registry import ToolRegistry


def read_call(identifier, path="note.txt", **extra):
    return ToolCall(identifier, "read", {"root": "files", "path": path, **extra})


@pytest.mark.parametrize("extra", [{"_reason": None}, {"_reason": ""}, {"_reason": " "}, {"_reason": "x" * 501}])
async def test_reason_repair_through_runtime_and_real_registry(monkeypatch, extra):
    initial = read_call("initial", **extra)
    repaired = read_call("repaired", _reason="Die Datei wird für die Antwort benötigt.")
    db, run, requests, _, _ = await _runtime_setup(
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
    assert message_dict(first) == message_dict(Message(role="tool", content=first.content, tool_call_id=first.tool_call_id))
    assert json.loads(first.content)["error"] == {
        "code": "tool.invalidArguments",
        "message": "The tool arguments are invalid. Inspect error.field and error.params.reason and retry with arguments that match the tool schema.",
        "field": "_reason",
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


@pytest.mark.parametrize("extra,valid", [({}, True), ({"_reason": None}, False), ({"_reason": " "}, False), ({"_reason": "Prüfen."}, True)])
def test_prompt_evaluator_accepts_missing_reason_and_validates_supplied_value(extra, valid):
    case = PromptCase(id="reason-contract", kind="general", user="Lies note.txt.", tools=("read",))
    assert (not _validate_tool_calls(case, [read_call("read", **extra)])) is valid
