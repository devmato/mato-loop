import asyncio
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest

from mato_loop_backend.agent import script_tools
from mato_loop_backend.agent.projection import _target_tool_result
from mato_loop_backend.agent.tool_round import session_edit_replay_key
from mato_loop_backend.settings import TrustLevel
from mato_loop_backend.settings.models import ModelConfig


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


@pytest.mark.parametrize(("ending", "code"), [
    ("return 'x'.repeat(16_001);", "tool.scriptOutputLimit"),
    ("throw new Error('private exception text');", "tool.scriptJavaScriptFailed"),
])
async def test_script_late_failure_preserves_progress(ending, code):
    steps = []

    async def dispatch(name, _args):
        steps.append({"tool": name, "messageId": str(uuid4()), "status": "completed"})

    result = await script_tools.run_script(
        script_tools.ScriptArguments(code=f"await callTool('read'); {ending}"), dispatch,
    )
    script_tools.with_script_progress(result, steps, 4)
    assert json.loads(result.content) == {
        "error": {"code": code, "message": json.loads(script_tools.ToolResult.error(code).content)["error"]["message"],
                  "params": {"toolCalls": 1}},
        "scriptSteps": steps, "runToolCalls": 4,
    }


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


async def test_script_with_no_calls_or_result_is_not_reported_as_success():
    result = await script_tools.run_script(script_tools.ScriptArguments(code="return;"), None)
    assert result.metadata["errorCode"] == "tool.scriptNoProgress"


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
