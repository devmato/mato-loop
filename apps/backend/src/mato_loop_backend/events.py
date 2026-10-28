from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import UUID

TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled"}
LIVE_EVENTS = {
    "run.started", "run.phase", "run.timing", "message.delta", "reasoning.updated",
    "context.summary.updated", "process.output", "run.warning",
    "message.completed", "message.interrupted", "message.continued", "context.compacted",
    "tool.completed",
}


class RunEventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[UUID, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._history: dict[UUID, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=200))
        self._checkpoints: dict[UUID, dict[str, Any]] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._subagents: dict[UUID, tuple[UUID, dict[str, Any]]] = {}

    def register_subagent(
        self, run_id: UUID, parent_run_id: UUID, summary: dict[str, Any]
    ) -> None:
        self._subagents[run_id] = (parent_run_id, dict(summary))

    def unregister_subagent(self, run_id: UUID) -> None:
        self._subagents.pop(run_id, None)

    async def publish(self, run_id: UUID, event: dict[str, Any]) -> None:
        self._record_checkpoint(run_id, event)
        if event.get("type") not in LIVE_EVENTS:
            self._history[run_id].append(event)
        for queue in tuple(self._subscribers[run_id]):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                for item in self._replay(run_id):
                    queue.put_nowait(item)
                if event.get("type") == "tool.completed":
                    queue.put_nowait(event)
            else:
                queue.put_nowait(event)
        if event.get("type") in TERMINAL_EVENTS:
            task = asyncio.create_task(self._cleanup(run_id))
            self._cleanup_tasks.add(task)
            task.add_done_callback(self._cleanup_tasks.discard)

        parent = self._subagents.get(run_id)
        if parent is not None:
            parent_run_id, summary = parent
            event_type = event.get("type", "")
            request_event = event_type.startswith(("approval.", "question."))
            if request_event:
                provenance = {
                    "subagentSessionId": summary["sessionId"],
                    "subagentTitle": summary["title"],
                }
                forwarded = {**event, **provenance}
                for key in ("approval", "question"):
                    if isinstance(event.get(key), dict):
                        forwarded[key] = {
                            **event[key],
                            "payload": {**(event[key].get("payload") or {}), **provenance},
                        }
                await self.publish(parent_run_id, forwarded)
            if request_event or event_type in {
                "run.started", "run.timing", "run.completed", "run.failed", "run.cancelled",
            }:
                # This is an invalidation hint; clients reload the durable child status.
                await self.publish(parent_run_id, {
                    "type": "subagent.updated", "sessionId": summary["sessionId"],
                    "runId": str(run_id), "toolCallId": summary["toolCallId"],
                })

    def _record_checkpoint(self, run_id: UUID, event: dict[str, Any]) -> None:
        state = self._checkpoints.setdefault(run_id, {
            "content": "", "events": {}, "sources": {},
        })
        events = state["events"]
        kind = event.get("type")
        key = (kind,)
        if kind == "message.delta":
            state["content"] += event["delta"]
            return
        if kind in {"message.completed", "message.interrupted", "message.continued"}:
            state["content"] = ""
            for previous in tuple(events):
                if previous[0] in {
                    "reasoning.updated", "context.summary.updated", "run.warning",
                    "run.phase", "tool.phase", "context.compacted", "interaction",
                }:
                    events.pop(previous)
            key = ("message",)
        elif kind == "context.compacted":
            events.pop(("context.summary.updated",), None)
        elif kind == "run.phase" and event.get("phase") == "processing_tool":
            events.pop(("run.phase",), None)
            key = ("tool.phase",)
        elif kind in {"approval.created", "question.created"}:
            request_kind = kind.split(".")[0]
            key = (kind, (event.get(request_kind) or {}).get("id"))
            if not event.get("subagentSessionId"):
                events.pop(("interaction",), None)
        elif kind in {"approval.resolved", "approval.expired", "question.answered", "question.rejected", "question.expired"}:
            request_kind = kind.split(".")[0]
            events.pop((f"{request_kind}.created", event.get(f"{request_kind}Id")), None)
            if event.get("subagentSessionId"):
                return
            key = ("interaction",)
        elif kind == "run.warning":
            key = (kind, event.get("code"), event.get("modelRequest"))
        elif kind == "process.output":
            key = (kind, event.get("toolCallId"))
            if "delta" in event:
                output = dict(events.get(key, {}).get("output", {"stdout": "", "stderr": ""}))
                stream = event["stream"]
                output[stream] = output.get(stream, "") + event["delta"]
                event = {"type": kind, "toolCallId": event["toolCallId"], "output": output}
        elif kind == "tool.completed":
            events.pop(("process.output", event.get("toolCallId")), None)
            for source in (event.get("metadata") or {}).get("sources") or []:
                if isinstance(source, dict) and isinstance(source.get("url"), str):
                    state["sources"][source["url"]] = source
            return
        elif kind not in LIVE_EVENTS:
            return
        # Replacement moves the checkpoint to its original event order.
        events.pop(key, None)
        events[key] = event

    def _snapshot(self, run_id: UUID) -> dict[str, Any]:
        state = self._checkpoints.get(run_id, {})
        return {
            "type": "run.snapshot", "runId": str(run_id), "content": state.get("content", ""),
            "events": deepcopy(list(state.get("events", {}).values())),
            "researchSources": deepcopy(list(state.get("sources", {}).values())),
        }

    def _replay(self, run_id: UUID) -> list[dict[str, Any]]:
        history = self._history.get(run_id, [])
        return [
            *(event for event in history if event.get("type") not in TERMINAL_EVENTS),
            self._snapshot(run_id),
            *(event for event in history if event.get("type") in TERMINAL_EVENTS),
        ]

    async def _cleanup(self, run_id: UUID) -> None:
        await asyncio.sleep(60)
        self._history.pop(run_id, None)
        self._checkpoints.pop(run_id, None)
        if not self._subscribers.get(run_id):
            self._subscribers.pop(run_id, None)

    async def subscribe(self, run_id: UUID) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        for event in self._replay(run_id):
            queue.put_nowait(event)
        self._subscribers[run_id].add(queue)
        try:
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield {"type": "keepalive"}
        finally:
            self._subscribers[run_id].discard(queue)
            if not self._subscribers[run_id]:
                self._subscribers.pop(run_id, None)


run_events = RunEventBroker()
