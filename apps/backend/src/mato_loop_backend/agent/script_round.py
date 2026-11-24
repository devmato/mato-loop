"""Coordinate a scripted tool call without owning tool execution or persistence."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from mato_loop_backend.agent.projection import (
    _model_file_tool_result,
    _target_tool_result,
    _tool_result_reference,
)
from mato_loop_backend.agent.script_tools import (
    ScriptArguments,
    StopScript,
    run_script,
    with_script_progress,
)
from mato_loop_backend.models import Message
from mato_loop_backend.privacy import PseudonymizationError
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings.models import TrustLevel
from mato_loop_backend.tools.contracts import ToolResult
from mato_loop_backend.tools.references import model_tool_references


@dataclass(frozen=True, slots=True)
class ScriptChildResult:
    message: Message
    content: str
    metadata: dict[str, Any]
    replay_key: tuple[str, str] | None
    stop: ToolResult | None


@dataclass(slots=True)
class ScriptRound:
    execute_child: Callable[[ToolCall], Awaitable[ScriptChildResult]]
    allowed_names: frozenset[str]
    trust_level: TrustLevel
    extra_call_budget: int | None
    restore_arguments: Callable[[dict[str, Any]], dict[str, Any]] | None
    source_review_pending: tuple[str, str] | None
    steps: list[dict[str, Any]] = field(default_factory=list, init=False)
    completed_actions: dict[tuple[str, str], UUID] = field(default_factory=dict, init=False)
    made_progress: bool = field(default=False, init=False)
    extra_calls: int = field(default=0, init=False)

    async def execute(self, arguments: ScriptArguments) -> ToolResult:
        first_child = True
        result_trust = self.trust_level

        async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
            nonlocal first_child, result_trust
            # The wrapper already occupies the first child's slot in the batch budget.
            if not first_child:
                if self.extra_call_budget is not None and self.extra_calls >= self.extra_call_budget:
                    raise StopScript(ToolResult.error("agent.toolCallLimitReached"))
                self.extra_calls += 1
            first_child = False
            if name not in self.allowed_names:
                raise StopScript(ToolResult.error("tool.unavailable"))
            if self.restore_arguments is not None:
                try:
                    arguments = self.restore_arguments(arguments)
                except PseudonymizationError as exc:
                    raise StopScript(ToolResult.error("privacy.pseudonymizationFailed")) from exc
            if self.source_review_pending is not None and (
                name != "read" or (arguments.get("root"), arguments.get("path")) != self.source_review_pending
            ):
                raise StopScript(ToolResult.error("tool.invalidArguments", field="code"))

            child = await self.execute_child(ToolCall(f"call-{uuid4()}", name, arguments))
            error = child.metadata.get("errorCode")
            deferred = child.metadata.get("repositoryInstructionsDeferred")
            if not error and not deferred and not child.metadata.get("alreadyCompleted"):
                self.made_progress = True
                if child.replay_key is not None:
                    self.completed_actions[child.replay_key] = child.message.id
            self.steps.append({
                "tool": name, "messageId": str(child.message.id),
                "status": "error" if error else "deferred" if deferred else "completed",
                **({"errorCode": error} if error else {}),
            })
            if child.message.trust_level == TrustLevel.PRIVATE.value:
                result_trust = TrustLevel.PRIVATE
            if child.stop is not None:
                raise StopScript(child.stop)
            if error == "permission.denied":
                raise StopScript(ToolResult.error("permission.denied"))
            if error in {"tool.scriptEditAlreadyApplied", "tool.scriptActionAlreadyApplied"}:
                raise StopScript(ToolResult(child.content, child.metadata))
            if child.message.trust_level == TrustLevel.PRIVATE.value and self.trust_level == TrustLevel.PUBLIC:
                # A script must not turn protected tool data into an untyped result.
                raise StopScript(ToolResult.error("permission.denied"))
            output = _target_tool_result(
                name, child.content, self.trust_level, source_trust_level=child.message.trust_level,
                live_history_stamp=child.metadata.get("liveHistoryProjection"),
            )
            output = model_tool_references(name, _model_file_tool_result(output), child.message.id)
            if len(output) > 96_000:
                return {
                    "outputTooLarge": True, "messageId": str(child.message.id),
                    "toolResultReference": _tool_result_reference(child.message.id, output),
                }
            try:
                return json.loads(output)
            except ValueError:
                return output

        result = await run_script(arguments, dispatch)
        result.trust_level = result_trust
        return result


def finish_script_result(
    script: ScriptRound | None, result: ToolResult, run_count: int,
    interrupted_actions: dict[tuple[str, str], UUID],
) -> ToolResult:
    steps = script.steps if script else []
    failed_steps = sum(step["status"] == "error" for step in steps)
    if failed_steps and not result.metadata.get("errorCode"):
        # Keep useful output even when a child failed.
        failure = ToolResult.error("tool.scriptStepFailed", params={"failedSteps": failed_steps})
        result.content = json.dumps({**json.loads(result.content), **json.loads(failure.content)}, ensure_ascii=False)
        result.metadata = {**result.metadata, **failure.metadata}
    with_script_progress(result, steps, run_count)
    if script and (result.metadata.get("errorCode") or any(step["status"] != "completed" for step in steps)):
        interrupted_actions.update(script.completed_actions)
    return result
