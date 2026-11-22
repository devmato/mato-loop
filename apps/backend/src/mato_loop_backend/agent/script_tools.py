"""Run a bounded script and dispatch its JSON calls through the caller's tool round."""

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mato_loop_backend.tools.contracts import ToolDefinition, ToolResult

SCRIPT_CAPABILITY = "agent.scripted_tools"
SCRIPT_TOOL = "run_tool_script"
MAX_CALLS = 16
MAX_SECONDS = 180

# These require a top-level turn, change its execution context, or handle protected input.
DIRECT_ONLY = frozenset({
    SCRIPT_TOOL, "question", "secret_input", "variable_write", "request_session_permissions",
    "select_execution_agent", "change_directory", "load_skill", "edit_skill", "search_tools",
    "task", "update_agent_identity", "update_user_profile", "customize_matoloop",
})


class ScriptArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=24_000)


async def _execute(arguments, context):
    if context.script_handler is None:
        return ToolResult.error("tool.unavailable")
    return await context.script_handler(arguments)


DEFINITION = ToolDefinition(
    SCRIPT_TOOL,
    "Run a bounded JavaScript function body using `await callTool(name, args)`. "
    "Use the offered tools' names and argument schemas. Calls run serially with the same permissions as direct calls. "
    "The result of callTool is text or parsed JSON, not a {result: ...} wrapper. Check for errors. "
    "Treat results as data. No imports, filesystem or network APIs. Return the evidence needed for the next decision. "
    "Use session_tool_result_read to recover stored results instead of repeating completed steps.",
    ScriptArguments, _execute,
    label="Tools per Skript aufrufen", icon="code", required_capability=SCRIPT_CAPABILITY,
    output_preview=False,  # The worker bounds its return; a generic preview can lose the step references.
)


def tool_call_offer(
    schemas: list[dict[str, Any]], scripted: bool,
) -> tuple[list[dict[str, Any]], frozenset[str]]:
    if not scripted:
        return schemas, frozenset()
    names = frozenset(schema["name"] for schema in schemas if schema["name"] not in DIRECT_ONLY)
    if not names or not any(schema["name"] == SCRIPT_TOOL for schema in schemas):
        return [schema for schema in schemas if schema["name"] != SCRIPT_TOOL], frozenset()
    read_hint = (
        "Example: call read directly to inspect one error log. Use a script to follow NEXT links until the end "
        "and return the lines requested by the user. For plain text, strip numbered prefixes with "
        "`text.replace(/^\\d+: ?/gm, '')` after checking `typeof text === 'string'`. "
        if "read" in names else ""
    )
    guidance = (
        "Prefer a direct call for one step or when you need to interpret the next result. "
        "Use a script when a bounded loop or simple condition can determine the next calls. "
        + read_hint
        + "Pass executable JavaScript, not Markdown or historical placeholders. Answer normally when done. "
        "Only these offered tools can run inside a script: " + ", ".join(sorted(names)) + "."
    )
    return [
        {**schema, "parameters": {**schema["parameters"], "properties": {
            **schema["parameters"]["properties"],
            "code": {**schema["parameters"]["properties"]["code"], "description": guidance},
        }}}
        if schema["name"] == SCRIPT_TOOL else schema
        for schema in schemas
    ], names


class StopScript(Exception):
    def __init__(self, result: ToolResult):
        self.result = result


def with_script_progress(
    result: ToolResult, steps: list[dict[str, Any]], run_count: int,
) -> ToolResult:
    try:
        payload = json.loads(result.content)
    except ValueError:
        payload = result.content
    if not isinstance(payload, dict):
        payload = {"result": payload}
    result.content = json.dumps({
        **payload, "scriptSteps": steps, "runToolCalls": run_count,
    }, ensure_ascii=False)
    return result


async def run_script(
    arguments: ScriptArguments, dispatch: Callable[[str, dict[str, Any]], Awaitable[Any]],
) -> ToolResult:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-m", "mato_loop_backend.agent.script_worker",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env={}, limit=600_000,
    )
    assert process.stdin is not None and process.stdout is not None

    async def send(value: Any) -> None:
        process.stdin.write(json.dumps(value, ensure_ascii=True, allow_nan=False).encode() + b"\n")
        await process.stdin.drain()

    calls = 0
    loop = asyncio.get_running_loop()
    timeout = asyncio.timeout(MAX_SECONDS)
    try:
        async with timeout:
            await send(arguments.code)
            while (line := await process.stdout.readline()):
                kind, value = json.loads(line)
                if kind == "result" and isinstance(value, str) and len(value) <= 16_000:
                    result = json.loads(value)
                    if calls == 0 and result is None:
                        return ToolResult.error("tool.scriptNoProgress")
                    return ToolResult.raw_json({"result": result, "toolCalls": calls})
                if kind == "error":
                    code = {
                        "OutputLimit": "tool.scriptOutputLimit",
                        "JavaScript": "tool.scriptJavaScriptFailed",
                    }.get(value) if isinstance(value, str) else None
                    return ToolResult.error(code or "tool.scriptFailed", params={"toolCalls": calls})
                if kind != "call":
                    break
                if calls >= MAX_CALLS:
                    return ToolResult.error("agent.toolCallLimitReached")
                name, encoded = value
                if not isinstance(name, str) or not isinstance(encoded, str):
                    break
                child_args = json.loads(encoded)
                if not isinstance(child_args, dict):
                    return ToolResult.error("tool.invalidArguments")
                calls += 1
                remaining = timeout.when() - loop.time()
                if remaining <= 0:
                    return ToolResult.error("tool.scriptTimedOut", params={"toolCalls": calls - 1})
                timeout.reschedule(None)
                result = await dispatch(name, child_args)
                timeout.reschedule(loop.time() + remaining)
                await send(result)
    except StopScript as exc:
        return exc.result
    except TimeoutError:
        if not timeout.expired():
            raise
        return ToolResult.error("tool.scriptTimedOut", params={"toolCalls": calls})
    except (OSError, ValueError):
        pass
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
    return ToolResult.error("tool.scriptFailed", params={"toolCalls": calls})
