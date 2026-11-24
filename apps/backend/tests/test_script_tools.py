import asyncio
import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from runtime_helpers import _assistant_messages, _runtime_setup, _tool_messages

import mato_loop_backend.tools.web as tools_web
from mato_loop_backend.agent import script_tools
from mato_loop_backend.agent.projection import _target_tool_result
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.agent.tool_round import session_edit_replay_key
from mato_loop_backend.models import Message, WebSearchPolicy
from mato_loop_backend.providers.base import ProviderResult, ToolCall
from mato_loop_backend.settings import TrustLevel
from mato_loop_backend.settings.models import ModelConfig
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.web.fetch import WebFetchResult


def test_script_opt_in_is_off_by_default_and_requires_tools():
    config = {"provider_id": uuid4(), "model_id": "local", "name": "Local"}
    assert not ModelConfig(**config).scripted_tools_enabled
    assert ModelConfig(**config, scriptedToolsEnabled=True).scripted_tools_enabled
    assert not ModelConfig(**config, supports_tools=False, scriptedToolsEnabled=True).scripted_tools_enabled


def test_script_mode_reuses_direct_schemas_and_keeps_controls_direct_only():
    schemas = [
        {"name": "read", "description": "Read a file", "parameters": {"type": "object"}},
        {"name": "question"},
        script_tools.DEFINITION.schema(),
    ]
    direct, script_names = script_tools.tool_call_offer(schemas, True)
    assert direct[:2] == schemas[:2]
    assert script_names == {"read"}
    assert direct[-1]["description"] == schemas[-1]["description"]
    description = direct[-1]["parameters"]["properties"]["code"]["description"]
    assert description.endswith("Only these offered tools can run inside a script: read.")
    assert "16 calls per script" in description and "16,000 characters of serialized JSON" in description
    assert "return the next cursor or offset" in description
    assert "Read a file" not in description
    assert script_tools.tool_call_offer(schemas[1:], True) == ([schemas[1]], frozenset())
    assert script_tools.tool_call_offer(schemas[:2], False) == (schemas[:2], frozenset())


async def test_script_chains_data_without_ambient_capabilities():
    async def dispatch(name, args):
        return {"next": {"list": "selected.txt", "read": args.get("path")}[name]}

    result = await script_tools.run_script(script_tools.ScriptArguments(code="""
        const item = await callTool('list');
        return [await callTool('read', {path: item.next}), typeof fetch, typeof process, typeof require];
    """), dispatch)
    assert json.loads(result.content) == {
        "result": [{"next": "selected.txt"}, "undefined", "undefined", "undefined"], "toolCalls": 2,
    }


@pytest.mark.usefixtures("skill_library")
async def test_script_pages_only_needed_chunks_of_oversized_webfetch(monkeypatch):
    url = "https://example.test/report"
    body = "x" * 14_000 + "Finding: cobalt." + "x" * 86_000
    monkeypatch.setattr(tools_web, "fetch_web", AsyncMock(
        return_value=WebFetchResult(url, "text/plain", body, 200, "text"),
    ))
    script = ToolCall("script", "run_tool_script", {"code": """
        const fetched = await callTool('webfetch', {url: 'https://example.test/report', format: 'text'});
        const reference = fetched.toolResultReference;
        if (!fetched.outputTooLarge || !reference) throw new Error('Missing stored result reference');
        let args = reference.arguments;
        while (true) {
            const page = await callTool(reference.tool, args);
            const finding = page.content.match(/Finding: ([a-z]+)/);
            if (finding) return {reference, finding: finding[1], nextOffset: page.nextOffset};
            if (!page.hasMore) throw new Error('Finding not found');
            args = {...args, offset: page.nextOffset};
        }
    """})
    # Use the runtime fixture's scripted model and database, but keep real tool handlers.
    execute = tool_registry._execute_definition
    db, run, *_ = await _runtime_setup(
        monkeypatch, [ProviderResult(tool_calls=[script]), ProviderResult(content="Done")],
        scripted_tools_enabled=True, search_policy=WebSearchPolicy.DISABLED, input_budget_tokens=100_000,
    )
    monkeypatch.setattr(tool_registry, "_execute_definition", execute)

    records = AgentRuntime._records.return_value

    def add(record):
        if isinstance(record, Message):
            # The mocked database does not assign IDs on flush.
            record.id = record.id or uuid4()
            records.append(record)

    def scalar(statement):
        if statement.column_descriptions[0]["expr"] is Message:
            params = statement.compile().params.values()
            return next((record for record in records if record.role == "tool" and record.id in params), None)
        return db.scalar.return_value

    db.add.side_effect = add
    db.scalar.side_effect = scalar
    db.scalars.side_effect = lambda statement: Mock(all=Mock(return_value=(
        list(records) if statement.column_descriptions[0]["expr"] is Message else []
    )))

    await AgentRuntime._execute_loop(db, run)

    source, *_, result = _tool_messages(db)
    assert json.loads(source.content)["content"] == body
    outcome = json.loads(result.content)
    assert "result" in outcome, outcome
    assert outcome["result"] == {
        "reference": {
            "tool": "session_tool_result_read",
            "arguments": {"message_id": str(source.id), "offset": 0},
            "totalCharacters": len(source.content),
        },
        "finding": "cobalt",
        "nextOffset": 24_000,
    }
    calls = _assistant_messages(db)[0].provider_data["_matoloop"]["scriptToolCalls"].values()
    assert [call["arguments"] for call in calls if call["name"] == "session_tool_result_read"] == [
        {"message_id": str(source.id), "offset": offset} for offset in (0, 12_000)
    ]


@pytest.mark.parametrize(("ending", "code", "params"), [
    ("return 'x'.repeat(16_001);", "tool.scriptOutputLimit",
     {"maxChars": 16_000, "hint": script_tools.OUTPUT_LIMIT_HINT}),
    ("throw new Error('private exception text');", "tool.scriptJavaScriptFailed", {"stage": "execution"}),
])
async def test_script_late_failure_preserves_progress(ending, code, params):
    steps = []

    async def dispatch(name, _args):
        steps.append({"tool": name, "messageId": str(uuid4()), "status": "completed"})

    result = await script_tools.run_script(
        script_tools.ScriptArguments(code=f"await callTool('read'); {ending}"), dispatch,
    )
    script_tools.with_script_progress(result, steps, 4)
    assert result.metadata["errorCode"] == code
    assert json.loads(result.content) == {
        "error": {"code": code, "message": json.loads(script_tools.ToolResult.error(code).content)["error"]["message"],
                  "params": {"toolCalls": 1, **params}},
        "scriptSteps": steps, "runToolCalls": 4,
    }


async def test_malformed_script_reports_only_controlled_compilation_stage():
    result = await script_tools.run_script(script_tools.ScriptArguments(code="const sensitiveSource = ;"), None)
    expected = script_tools.ToolResult.error(
        "tool.scriptJavaScriptFailed", params={"toolCalls": 0, "stage": "compilation"},
    )
    assert result == expected


async def test_script_cannot_catch_host_denial_and_continue():
    calls = []

    async def dispatch(name, _args):
        calls.append(name)
        raise script_tools.StopScript(script_tools.ToolResult.error("permission.userDenied"))

    result = await script_tools.run_script(script_tools.ScriptArguments(code="""
        try { await callTool('first'); } catch {}
        await callTool('second');
    """), dispatch)
    assert calls == ["first"] and result.metadata["errorCode"] == "permission.userDenied"


@pytest.mark.parametrize("makes_call", [False, True])
async def test_script_can_return_null_only_if_child_calls_were_made(makes_call):
    async def dispatch(_name, _args):
        return {"written": "report.md"}

    source = ("await callTool('write_file'); " if makes_call else "") + "return null;"
    result = await script_tools.run_script(script_tools.ScriptArguments(code=source), dispatch)
    expected = (
        script_tools.ToolResult.raw_json({"result": None, "toolCalls": 1}) if makes_call
        else script_tools.ToolResult.error("tool.scriptNoProgress")
    )
    assert result == expected


async def test_script_runaway_loop_times_out(monkeypatch):
    monkeypatch.setattr(script_tools, "MAX_SECONDS", 0.5)
    result = await asyncio.wait_for(script_tools.run_script(
        script_tools.ScriptArguments(code="while (true) {}"), None,
    ), timeout=5)
    assert result.metadata["errorCode"] == "tool.scriptTimedOut"


async def test_script_cancel_stops_waiting_worker():
    waiting = asyncio.Event()

    async def dispatch(_name, _args):
        waiting.set()
        await asyncio.Future()

    task = asyncio.create_task(script_tools.run_script(
        script_tools.ScriptArguments(code="await callTool('read');"), dispatch,
    ))
    await asyncio.wait_for(waiting.wait(), 10)
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    assert task.cancelled()


async def test_script_budget_does_not_count_tool_wait(monkeypatch):
    monkeypatch.setattr(script_tools, "MAX_SECONDS", 2)

    async def dispatch(_name, _args):
        await asyncio.sleep(2.1)
        return 42

    result = await script_tools.run_script(
        script_tools.ScriptArguments(code="return await callTool('read');"), dispatch,
    )
    assert json.loads(result.content) == {"result": 42, "toolCalls": 1}


def test_private_script_output_is_not_replayed_to_public_model():
    result = _target_tool_result("run_tool_script", '{"result":"private"}', TrustLevel.PUBLIC,
                                 source_trust_level="private")
    assert json.loads(result) == {"redactedPrivateContext": True}


def test_script_edit_replay_identity_ignores_permission_reason_and_root_alias():
    files = Mock()
    files.normalize_relative_path.side_effect = lambda path: path
    args = {"root": "files", "path": "example.txt", "oldString": "X", "newString": "XY"}
    key = session_edit_replay_key("edit_file", args, files)
    assert key and key == session_edit_replay_key(
        "edit_file", {**args, "root": "session", "replaceAll": False, "_reason": "A retry"}, files,
    )
