import asyncio
from contextlib import aclosing
from uuid import uuid4

import pytest

from mato_loop_backend.events import RunEventBroker


async def test_snapshot_restores_full_text_after_history_eviction_and_then_live_deltas():
    broker = RunEventBroker()
    run_id = uuid4()
    await broker.publish(run_id, {"type": "run.started", "modelName": "Model"})
    await broker.publish(run_id, {"type": "run.phase", "phase": "waiting_model"})
    text = "".join(f"[{index}]" for index in range(350))
    for index in range(350):
        await broker.publish(run_id, {"type": "message.delta", "delta": f"[{index}]"})
        await broker.publish(run_id, {"type": "message.updated", "messageId": str(index)})

    async with aclosing(broker.subscribe(run_id)) as stream:
        notifications = [await anext(stream) for _ in range(200)]
        assert notifications[0] == {"type": "message.updated", "messageId": "150"}
        snapshot = await anext(stream)
        assert len(broker._history[run_id]) == 200
        assert snapshot["type"] == "run.snapshot"
        assert snapshot["content"] == text
        await broker.publish(run_id, {"type": "message.delta", "delta": " next"})
        assert await anext(stream) == {"type": "message.delta", "delta": " next"}
        assert snapshot["content"] == text


@pytest.mark.parametrize("kind", ["message.delta", "process.output"])
async def test_slow_subscriber_receives_a_snapshot_without_duplicating_the_overflowing_delta(kind):
    broker = RunEventBroker()
    run_id = uuid4()
    event = {"type": kind}
    if kind == "process.output":
        event.update({"toolCallId": "shell", "stream": "stdout"})
    async with aclosing(broker.subscribe(run_id)) as stream:
        await anext(stream)
        for _ in range(501):
            await broker.publish(run_id, {**event, "delta": "x"})
        snapshot = await anext(stream)
        assert snapshot["type"] == "run.snapshot"
        text = snapshot["content"] if kind == "message.delta" else snapshot["events"][0]["output"]["stdout"]
        assert text == "x" * 501
        await broker.publish(run_id, {**event, "delta": "y"})
        assert await anext(stream) == {**event, "delta": "y"}


@pytest.mark.parametrize("boundary", ["message.completed", "message.interrupted", "message.continued"])
async def test_message_boundaries_remove_the_previous_live_buffers(boundary):
    broker = RunEventBroker()
    run_id = uuid4()
    for event in [
        {"type": "message.delta", "delta": "Old answer"},
        {"type": "reasoning.updated", "reasoning": {"text": "Old reasoning"}},
        {"type": "run.warning", "code": "agent.toolCallLimitReached"},
        {"type": boundary, "message": {"content": "Old answer"}},
        {"type": "run.phase", "phase": "waiting_model"},
        {"type": "message.delta", "delta": "New answer"},
    ]:
        await broker.publish(run_id, event)
    async with aclosing(broker.subscribe(run_id)) as stream:
        snapshot = await anext(stream)
        assert snapshot["content"] == "New answer"
        assert not any(event["type"] in {"reasoning.updated", "run.warning"} for event in snapshot["events"])


async def test_finished_compaction_is_not_replayed_as_live_activity():
    broker = RunEventBroker()
    run_id = uuid4()
    for event in [
        {"type": "run.phase", "phase": "compacting_context"},
        {"type": "context.summary.updated", "summary": {"content": "Summary"}},
        {"type": "context.compacted", "summary": {"id": "summary", "content": "Summary"}},
        {"type": "run.phase", "phase": "waiting_model", "modelRequest": 2},
    ]:
        await broker.publish(run_id, event)
    async with aclosing(broker.subscribe(run_id)) as stream:
        snapshot = await anext(stream)
        assert snapshot["type"] == "run.snapshot"
        assert not any(event["type"] == "context.summary.updated" for event in snapshot["events"])
        assert snapshot["events"][-1] == {"type": "run.phase", "phase": "waiting_model", "modelRequest": 2}


async def test_snapshot_replays_original_events_and_releases_finished_process_output():
    broker = RunEventBroker()
    run_id = uuid4()
    for event in [
        {"type": "run.phase", "phase": "processing_tool", "tool": "shell_run", "toolCallId": "shell"},
        {"type": "process.output", "toolCallId": "shell", "output": {"stdout": "Out", "stderr": ""}},
        {"type": "process.output", "toolCallId": "shell", "stream": "stderr", "delta": "Error"},
        {"type": "process.output", "toolCallId": "shell", "stream": "stdout", "delta": "put"},
        {"type": "run.phase", "phase": "waiting_model", "modelRequest": 2},
        {"type": "reasoning.updated", "reasoning": {"text": "Thinking"}, "activityLabel": "Prüfen"},
        {"type": "run.timing", "activeTimingRevision": 3, "activeDurationMs": 1000, "activeDurationRunning": True},
    ]:
        await broker.publish(run_id, event)
    async with aclosing(broker.subscribe(run_id)) as stream:
        snapshot = await anext(stream)
        assert [event["type"] for event in snapshot["events"]] == [
            "run.phase", "process.output", "run.phase", "reasoning.updated", "run.timing",
        ]
        assert snapshot["events"][1]["output"] == {"stdout": "Output", "stderr": "Error"}
        await broker.publish(run_id, {"type": "tool.completed", "toolCallId": "shell", "metadata": {"sources": [{"url": "https://example.com"}]}})
        snapshot = broker._snapshot(run_id)
        assert not any(event["type"] == "process.output" for event in snapshot["events"])
        assert snapshot["researchSources"] == [{"url": "https://example.com"}]


async def test_terminal_event_survives_overflow_and_snapshot_state_is_cleaned_up(monkeypatch):
    broker = RunEventBroker()
    run_id = uuid4()
    stream = broker.subscribe(run_id)
    try:
        await anext(stream)
        for _ in range(500):
            await broker.publish(run_id, {"type": "message.delta", "delta": "x"})
        await broker.publish(run_id, {"type": "run.completed"})
        assert (await anext(stream))["type"] == "run.snapshot"
        assert await anext(stream) == {"type": "run.completed"}
    finally:
        await stream.aclose()
        for task in broker._cleanup_tasks:
            task.cancel()
        await asyncio.gather(*broker._cleanup_tasks, return_exceptions=True)

    async def no_delay(_seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    await broker._cleanup(run_id)
    assert run_id not in broker._checkpoints
    assert run_id not in broker._history


async def test_pending_child_approval_survives_history_eviction_without_changing_parent_phase():
    broker = RunEventBroker()
    run_id = uuid4()
    phase = {"type": "run.phase", "phase": "waiting_model"}
    approval = {"type": "approval.created", "approval": {"id": "approval"}, "subagentSessionId": "child"}
    await broker.publish(run_id, phase)
    await broker.publish(run_id, approval)
    for index in range(350):
        await broker.publish(run_id, {"type": "message.updated", "messageId": str(index)})
    assert broker._snapshot(run_id)["events"] == [phase, approval]
    await broker.publish(run_id, {"type": "approval.resolved", "approvalId": "approval", "subagentSessionId": "child"})
    assert broker._snapshot(run_id)["events"] == [phase]
