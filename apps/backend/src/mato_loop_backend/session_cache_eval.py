from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select

from mato_loop_backend.auth.sessions import create_login_session, remove_login_session
from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.db.session import AsyncSessionFactory, engine
from mato_loop_backend.models import AgentRun, ChatSession, Message, UserAccount
from mato_loop_backend.sessions.service import ACTIVE_RUN_STATUSES
from mato_loop_backend.settings import (
    effective_model_pseudonymization,
    effective_model_trust,
    load_effective_ai,
)
from mato_loop_backend.settings.models import ReasoningEffort

SESSION_CODES = (
    ("K314159", "K271828", "K161803"),
    ("K141421", "K173205", "K223606"),
    ("K244949", "K264575", "K282842"),
    ("K316227", "K331662", "K346410"),
)
SEQUENCE = (
    ("A", 1, "new_session", 0),
    ("A", 2, "same_session_followup", 0),
    ("B", 1, "new_session", 0),
    ("A", 3, "return_after_switch", 1),
    ("C", 1, "new_session", 0),
    ("B", 2, "return_after_switch", 2),
    ("A", 4, "return_after_switch", 2),
    ("C", 2, "return_after_switch", 2),
)
FOLLOW_UPS = {
    2: "Nenne ausschließlich die Sitzungskennung aus meiner ersten Nachricht.",
    3: "Wiederhole ausschließlich dieselbe Sitzungskennung, ohne Erklärung.",
    4: "Gib ausschließlich die weiterhin gültige Sitzungskennung aus.",
}
TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_TITLE_STATUSES = {"completed", "failed", "skipped"}
DEFAULT_MODELS = {
    "lemonade": "Qwen3.6-35B-A3B-MTP-GGUF",
    "openai-codex": "openai-codex/gpt-5.6-sol",
}


def _number_stats(values: list[float | int]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "median": round(float(statistics.median(values)), 3),
        "min": round(float(min(values)), 3),
        "max": round(float(max(values)), 3),
    }


def _cache_metrics(
    input_tokens: int, cached_tokens: int, previous: dict[str, Any] | None
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "rawCacheHitRate": round(cached_tokens / input_tokens, 4) if input_tokens else None,
        "cacheablePrefixTokens": None,
        "reusablePrefixCoverage": None,
        "avoidableReprocessedTokens": None,
        "newInputTokens": None,
    }
    if previous is None:
        return values
    cacheable = min(
        input_tokens,
        int(previous["inputTokens"]) + int(previous["outputTokens"]),
    )
    values.update(
        {
            "cacheablePrefixTokens": cacheable,
            "reusablePrefixCoverage": (
                round(min(cached_tokens, cacheable) / cacheable, 4) if cacheable else None
            ),
            "avoidableReprocessedTokens": max(0, cacheable - cached_tokens),
            "newInputTokens": max(0, input_tokens - cacheable),
        }
    )
    return values


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[record["phase"]].append(record)

    phases = {}
    for phase, rows in groups.items():
        phases[phase] = {
            "requests": len(rows),
            "passed": sum(bool(row["passed"]) for row in rows),
            "inputTokens": _number_stats([row["inputTokens"] for row in rows]),
            "cachedInputTokens": _number_stats([row["cachedInputTokens"] for row in rows]),
            "cacheWriteInputTokens": _number_stats(
                [
                    row["cacheWriteInputTokens"]
                    for row in rows
                    if row["cacheWriteInputTokens"] is not None
                ]
            ),
            "rawCacheHitRate": _number_stats(
                [row["rawCacheHitRate"] for row in rows if row["rawCacheHitRate"] is not None]
            ),
            "reusablePrefixCoverage": _number_stats(
                [
                    row["reusablePrefixCoverage"]
                    for row in rows
                    if row["reusablePrefixCoverage"] is not None
                ]
            ),
            "avoidableReprocessedTokens": _number_stats(
                [
                    row["avoidableReprocessedTokens"]
                    for row in rows
                    if row["avoidableReprocessedTokens"] is not None
                ]
            ),
            "timeToFirstSignalMs": _number_stats(
                [
                    row["timeToFirstSignalMs"]
                    for row in rows
                    if row["timeToFirstSignalMs"] is not None
                ]
            ),
            "providerDurationMs": _number_stats(
                [
                    row["providerDurationMs"]
                    for row in rows
                    if row["providerDurationMs"] is not None
                ]
            ),
            "elapsedSeconds": _number_stats([row["elapsedSeconds"] for row in rows]),
        }

    replicate_outcomes = []
    for replicate in sorted({int(row["replicate"]) for row in records}):
        current = [row for row in records if row["replicate"] == replicate]
        immediate = next(
            (row for row in current if row["phase"] == "same_session_followup"), None
        )
        returns = [row for row in current if row["phase"] == "return_after_switch"]
        coverages = [
            row["reusablePrefixCoverage"]
            for row in returns
            if row["reusablePrefixCoverage"] is not None
        ]
        avoidable = [
            row["avoidableReprocessedTokens"]
            for row in returns
            if row["avoidableReprocessedTokens"] is not None
        ]
        penalties = [
            row["timeToFirstSignalMs"] - immediate["timeToFirstSignalMs"]
            for row in returns
            if immediate is not None
            and immediate["timeToFirstSignalMs"] is not None
            and row["timeToFirstSignalMs"] is not None
        ]
        median_coverage = float(statistics.median(coverages)) if coverages else None
        median_avoidable = float(statistics.median(avoidable)) if avoidable else None
        median_penalty = float(statistics.median(penalties)) if penalties else None
        bad = bool(
            median_avoidable is not None
            and median_avoidable >= 512
            and (
                (median_coverage is not None and median_coverage < 0.95)
                or (median_penalty is not None and median_penalty >= 500)
            )
        )
        replicate_outcomes.append(
            {
                "replicate": replicate,
                "medianReturnPrefixCoverage": (
                    round(median_coverage, 4) if median_coverage is not None else None
                ),
                "medianReturnAvoidableTokens": median_avoidable,
                "medianReturnFirstSignalPenaltyMs": median_penalty,
                "cacheLoss": bad,
            }
        )

    returned = [row for row in records if row["phase"] == "return_after_switch"]
    returned_coverages = [
        row["reusablePrefixCoverage"]
        for row in returned
        if row["reusablePrefixCoverage"] is not None
    ]
    returned_avoidable = [
        row["avoidableReprocessedTokens"]
        for row in returned
        if row["avoidableReprocessedTokens"] is not None
    ]
    bad_replicates = sum(bool(item["cacheLoss"]) for item in replicate_outcomes)
    median_coverage = (
        float(statistics.median(returned_coverages)) if returned_coverages else None
    )
    median_avoidable = float(statistics.median(returned_avoidable)) if returned_avoidable else None
    if bad_replicates >= 2:
        decision = "investigate"
    elif (
        median_coverage is not None
        and median_coverage >= 0.98
        and median_avoidable is not None
        and median_avoidable < 512
    ):
        decision = "no_change"
    else:
        decision = "inconclusive"
    return {
        "requests": len(records),
        "passed": sum(bool(record["passed"]) for record in records),
        "phases": phases,
        "replicates": replicate_outcomes,
        "decision": decision,
    }


def _report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    prompt_cache_key = (
        "chat session ID"
        if payload["model"]["providerAdapter"] == "openai-codex"
        else "not sent (provider-managed cache)"
    )
    lines = [
        "# MatoLoop Natural Session Cache Evaluation",
        "",
        f"Model: `{payload['model']['modelId']}`",
        f"Provider: `{payload['model']['providerAdapter']}`",
        f"Trust: `{payload['model']['trustLevel']}`",
        f"Pseudonymization: `{str(payload['model']['pseudonymization']).lower()}`",
        f"Reasoning effort: `{payload['model']['reasoningEffort']}`",
        f"Prompt cache key: `{prompt_cache_key}`",
        f"Title model: `{payload['model']['titleModelId'] or payload['model']['modelId']}`",
        f"Sequence: `{' -> '.join(payload['sequence'])}`",
        f"Evaluator elapsed: {payload['elapsedSeconds']:.3f} s",
        f"Recorded agent-run duration: {payload['recordedAgentRunSeconds']:.3f} s (title requests excluded)",
        f"Result: {summary['passed']}/{summary['requests']} passed",
        f"Decision: `{summary['decision']}`",
        "",
        "Prefix coverage and avoidable tokens are estimates based on the previous request's token "
        "counts, not a comparison of the serialized provider prefix. A cache-write value of `-` "
        "means the provider did not report that metric.",
        "",
        "## Phase Summary",
        "",
        "| Phase | Passed | Read / write / input median | Prefix coverage median | Avoidable median | First signal median | Elapsed median |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for phase in ("new_session", "same_session_followup", "return_after_switch"):
        row = summary["phases"].get(phase)
        if row is None:
            continue
        cached = row["cachedInputTokens"]["median"] if row["cachedInputTokens"] else 0
        written = row["cacheWriteInputTokens"]["median"] if row["cacheWriteInputTokens"] else None
        written_text = f"{written:.0f}" if written is not None else "-"
        input_tokens = row["inputTokens"]["median"] if row["inputTokens"] else 0
        coverage = row["reusablePrefixCoverage"]
        avoidable = row["avoidableReprocessedTokens"]
        first_signal = row["timeToFirstSignalMs"]
        elapsed = row["elapsedSeconds"]
        lines.append(
            f"| {phase} | {row['passed']}/{row['requests']} | {cached:.0f} / {written_text} / {input_tokens:.0f} | "
            f"{coverage['median']:.1%} | {avoidable['median']:.0f} | "
            f"{first_signal['median']:.0f} ms | {elapsed['median']:.3f} s |"
            if coverage and avoidable and first_signal and elapsed
            else f"| {phase} | {row['passed']}/{row['requests']} | {cached:.0f} / {written_text} / {input_tokens:.0f} | - | - | - | - |"
        )
    lines.extend(
        [
            "",
            "## Per Request",
            "",
            "| Rep | Step | Phase | Passed | Read / write / input | Prefix coverage | Avoidable | First signal | Provider | Elapsed |",
            "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in payload["records"]:
        coverage = row["reusablePrefixCoverage"]
        cache_write = row["cacheWriteInputTokens"]
        lines.append(
            f"| {row['replicate']} | {row['step']} | {row['phase']} | "
            f"{'yes' if row['passed'] else 'no'} | {row['cachedInputTokens']} / "
            f"{cache_write if cache_write is not None else '-'} / {row['inputTokens']} | "
            f"{f'{coverage:.1%}' if coverage is not None else '-'} | "
            f"{row['avoidableReprocessedTokens'] if row['avoidableReprocessedTokens'] is not None else '-'} | "
            f"{row['timeToFirstSignalMs'] if row['timeToFirstSignalMs'] is not None else '-'} ms | "
            f"{row['providerDurationMs'] if row['providerDurationMs'] is not None else '-'} ms | "
            f"{row['elapsedSeconds']:.3f} s |"
        )
    lines.extend(["", "## Replicate Decision Data", "", "```json"])
    lines.append(json.dumps(summary["replicates"], ensure_ascii=False, indent=2))
    lines.extend(["```", ""])
    if payload.get("diagnostic"):
        lines.extend(
            [
                "## Memory-disabled Diagnostic",
                "",
                f"Decision: `{payload['diagnostic']['summary']['decision']}`",
                "",
            ]
        )
    return "\n".join(lines)


class SessionCacheEvaluator:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.user_id: UUID | None = None
        self.token: str | None = None
        self.model_id: UUID | None = None
        self.model_metadata: dict[str, Any] = {}
        self.reasoning_effort: str | None = None
        self.session_ids: list[UUID] = []
        self.client: httpx.AsyncClient | None = None

    async def prepare(self) -> None:
        async with AsyncSessionFactory() as db:
            users = list(
                (
                    await db.scalars(
                        select(UserAccount)
                        .where(UserAccount.is_active.is_(True))
                        .order_by(UserAccount.created_at, UserAccount.id)
                    )
                ).all()
            )
            if self.args.user_id:
                requested = UUID(self.args.user_id)
                users = [user for user in users if user.id == requested]
            elif len(users) > 1:
                admins = [user for user in users if user.is_admin]
                if len(admins) == 1:
                    users = admins
            if len(users) != 1:
                raise RuntimeError(
                    "Expected one active development user or one unique active admin; "
                    "pass --user-id when multiple candidates exist."
                )
            user = users[0]
            self.user_id = user.id
            active_runs = await db.scalar(
                select(func.count(AgentRun.id))
                .join(ChatSession, ChatSession.id == AgentRun.session_id)
                .where(
                    ChatSession.user_id == user.id,
                    AgentRun.parent_run_id.is_(None),
                    AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                )
            )
            if active_runs and not self.args.recover:
                raise RuntimeError("The selected user has an active agent run; retry when it is idle.")
            ai = await load_effective_ai(db, user.id)
            providers = {provider.id: provider for provider in ai.providers if provider.enabled}
            requested_model = self.args.model or DEFAULT_MODELS[self.args.adapter]
            candidates = [
                (model, provider)
                for model in ai.models
                if model.enabled
                and model.model_id == requested_model
                and (provider := providers.get(model.provider_id)) is not None
                and provider.adapter_type == self.args.adapter
            ]
            if len(candidates) != 1:
                raise RuntimeError(
                    f"Expected one enabled {self.args.adapter} model named "
                    f"{requested_model!r}, found {len(candidates)}."
                )
            model, provider = candidates[0]
            title_model = next(
                (
                    item
                    for item in ai.models
                    if item.id == (ai.title_generation_model_id or model.id)
                ),
                None,
            )
            self.model_id = model.id
            self.reasoning_effort = self.args.reasoning_effort or (
                "none" if provider.adapter_type == "lemonade" else None
            )
            self.model_metadata = {
                "modelId": model.model_id,
                "modelConfigId": str(model.id),
                "providerAdapter": provider.adapter_type,
                "trustLevel": effective_model_trust(provider, model).value,
                "pseudonymization": effective_model_pseudonymization(provider, model),
                "reasoningEffort": self.reasoning_effort or "model_default",
                "titleModelId": title_model.model_id if title_model is not None else None,
            }
            self.token, _expires_at = await create_login_session(db, user)
            await db.commit()
        config = get_runtime_config()
        self.client = httpx.AsyncClient(
            base_url=self.args.api_base.rstrip("/") + "/",
            cookies={config.session_cookie_name: self.token},
            timeout=httpx.Timeout(30),
        )
        await self._json("GET", "/auth/me")

    async def _json(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        assert self.client is not None
        response = await self.client.request(method, path.lstrip("/"), json=payload)
        if response.is_error:
            try:
                error = response.json().get("error", {})
                detail = error.get("code", "unknown")
            except (ValueError, AttributeError):
                detail = "invalid error response"
            raise RuntimeError(f"{method} {path} failed with HTTP {response.status_code}: {detail}")
        return response.json() if response.content else {}

    async def _wait_run(self, session_id: UUID, run_id: UUID) -> tuple[AgentRun, Message]:
        deadline = time.monotonic() + self.args.run_timeout
        while True:
            state = await self._json("GET", f"/sessions/{session_id}/runs/{run_id}")
            if state["status"] in TERMINAL_RUN_STATUSES:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Agent run {run_id} timed out.")
            await asyncio.sleep(0.2)
        async with AsyncSessionFactory() as db:
            run = await db.get(AgentRun, run_id)
            if run is None or run.status != "completed" or run.result_message_id is None:
                raise RuntimeError(f"Agent run {run_id} ended with {state['status']}.")
            message = await db.get(Message, run.result_message_id)
            if message is None:
                raise RuntimeError(f"Agent run {run_id} has no result message.")
            return run, message

    async def _wait_title(self, session_id: UUID) -> str:
        deadline = time.monotonic() + self.args.title_timeout
        while True:
            state = await self._json("GET", f"/sessions/{session_id}")
            status = state["titleGenerationStatus"]
            if status in TERMINAL_TITLE_STATUSES:
                return status
            if time.monotonic() >= deadline:
                return "timeout"
            await asyncio.sleep(0.2)

    async def _create_session(self, content: str, memory_mode: str) -> tuple[UUID, UUID]:
        assert self.model_id is not None
        defaults = await self._json(
            "POST", "/sessions/defaults", payload={"model_id": str(self.model_id), "memory_mode": memory_mode}
        )
        payload = {
            "content": content,
            "run_id": str(uuid4()),
            "model_id": str(self.model_id),
            "memory_mode": memory_mode,
            "web_search_policy": defaults["webSearchPolicy"],
            "preferred_execution_agent_id": defaults["preferredExecutionAgentId"],
            **(
                {"current_directory": defaults["currentDirectory"]}
                if defaults["preferredExecutionAgentId"] is not None
                else {}
            ),
        }
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        response = await self._json("POST", "/sessions", payload=payload)
        session_id = UUID(response["session"]["id"])
        self.session_ids.append(session_id)
        return session_id, UUID(response["runId"])

    async def _send(self, session_id: UUID, content: str) -> UUID:
        response = await self._json(
            "POST",
            f"/sessions/{session_id}/messages",
            payload={"content": content, "run_id": str(uuid4())},
        )
        return UUID(response["runId"])

    async def _record(
        self,
        *,
        replicate: int,
        label: str,
        turn: int,
        phase: str,
        foreign_requests: int,
        expected: str,
        session_id: UUID,
        run_id: UUID,
        started: float,
        previous: dict[str, Any] | None,
        title_status: str | None,
    ) -> dict[str, Any]:
        run, message = await self._wait_run(session_id, run_id)
        provider_data = message.provider_data or {}
        usage = provider_data.get("usage") if isinstance(provider_data.get("usage"), dict) else {}
        matoloop = (
            provider_data.get("_matoloop")
            if isinstance(provider_data.get("_matoloop"), dict)
            else {}
        )
        performance = (
            matoloop.get("performance")
            if isinstance(matoloop.get("performance"), dict)
            else {}
        )
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        cached_tokens = int(usage.get("cached_input_tokens", 0))
        cache_write_value = usage.get("cache_write_input_tokens")
        cache_write_tokens = int(cache_write_value) if isinstance(cache_write_value, int) else None
        cache = _cache_metrics(input_tokens, cached_tokens, previous)
        normalized_answer = message.content.strip().strip("`").strip()
        return {
            "replicate": replicate,
            "step": f"{label}{turn}",
            "sessionLabel": label,
            "turn": turn,
            "phase": phase,
            "foreignChatRequests": foreign_requests,
            "sessionId": str(session_id),
            "runId": str(run_id),
            "passed": normalized_answer == expected,
            "expected": expected,
            "answer": message.content,
            "runTrustLevel": run.trust_level,
            "runPseudonymization": run.pseudonymization_enabled,
            "titleGenerationStatus": title_status,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cachedInputTokens": cached_tokens,
            "cacheWriteInputTokens": cache_write_tokens,
            **cache,
            "modelRequest": performance.get("modelRequest"),
            "effectiveToolCount": performance.get("effectiveToolCount"),
            "offeredToolCount": performance.get("offeredToolCount"),
            "estimatedInputTokens": performance.get("estimatedInputTokens"),
            "contextPreparationMs": performance.get("contextPreparationMs"),
            "timeToFirstSignalMs": performance.get("timeToFirstSignalMs"),
            "providerDurationMs": performance.get("providerDurationMs"),
            "elapsedSeconds": round(time.monotonic() - started, 3),
        }

    async def warmup(self, memory_mode: str) -> None:
        session_id, run_id = await self._create_session(
            "Kurzer Systemtest. Antworte ausschließlich mit: WARM", memory_mode
        )
        await self._wait_run(session_id, run_id)
        await self._wait_title(session_id)

    async def recover(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        assert self.user_id is not None
        code_locations = {
            code: (replicate, label)
            for replicate, codes in enumerate(SESSION_CODES, start=1)
            for label, code in zip(("A", "B", "C"), codes, strict=True)
        }
        records: list[dict[str, Any]] = []
        diagnostic: list[dict[str, Any]] = []
        async with AsyncSessionFactory() as db:
            sessions = list(
                (
                    await db.scalars(
                        select(ChatSession)
                        .join(Message, Message.session_id == ChatSession.id)
                        .where(
                            ChatSession.user_id == self.user_id,
                            Message.role == "user",
                            (
                                Message.content.startswith("Dies ist ein kurzer Cache-Test.")
                                | Message.content.startswith("Kurzer Systemtest.")
                            ),
                        )
                        .distinct()
                        .order_by(ChatSession.created_at, ChatSession.id)
                    )
                ).all()
            )
            self.session_ids.extend(session.id for session in sessions)
            for session in sessions:
                users = list(
                    (
                        await db.scalars(
                            select(Message)
                            .where(Message.session_id == session.id, Message.role == "user")
                            .order_by(Message.created_at, Message.id)
                        )
                    ).all()
                )
                if not users or users[0].content.startswith("Kurzer Systemtest."):
                    continue
                match = re.search(r"K\d{6}", users[0].content)
                if match is None or match.group() not in code_locations:
                    continue
                code = match.group()
                replicate, label = code_locations[code]
                runs = list(
                    (
                        await db.scalars(
                            select(AgentRun)
                            .where(
                                AgentRun.session_id == session.id,
                                AgentRun.parent_run_id.is_(None),
                            )
                            .order_by(AgentRun.created_at, AgentRun.id)
                        )
                    ).all()
                )
                by_input = {run.input_message_id: run for run in runs}
                previous = None
                for turn, user_message in enumerate(users, start=1):
                    run = by_input.get(user_message.id)
                    if run is None or run.status != "completed" or run.result_message_id is None:
                        continue
                    message = await db.get(Message, run.result_message_id)
                    if message is None:
                        continue
                    provider_data = message.provider_data or {}
                    usage = (
                        provider_data.get("usage")
                        if isinstance(provider_data.get("usage"), dict)
                        else {}
                    )
                    matoloop = (
                        provider_data.get("_matoloop")
                        if isinstance(provider_data.get("_matoloop"), dict)
                        else {}
                    )
                    performance = (
                        matoloop.get("performance")
                        if isinstance(matoloop.get("performance"), dict)
                        else {}
                    )
                    input_tokens = int(usage.get("input_tokens", 0))
                    output_tokens = int(usage.get("output_tokens", 0))
                    cached_tokens = int(usage.get("cached_input_tokens", 0))
                    cache_write_value = usage.get("cache_write_input_tokens")
                    cache_write_tokens = (
                        int(cache_write_value) if isinstance(cache_write_value, int) else None
                    )
                    cache = _cache_metrics(input_tokens, cached_tokens, previous)
                    phase = (
                        "new_session"
                        if turn == 1
                        else "same_session_followup"
                        if label == "A" and turn == 2
                        else "return_after_switch"
                    )
                    foreign_requests = (
                        0 if phase != "return_after_switch" else 1 if label == "A" and turn == 3 else 2
                    )
                    record = {
                        "replicate": replicate,
                        "step": f"{label}{turn}",
                        "sessionLabel": label,
                        "turn": turn,
                        "phase": phase,
                        "foreignChatRequests": foreign_requests,
                        "sessionId": str(session.id),
                        "runId": str(run.id),
                        "passed": message.content.strip().strip("`").strip() == code,
                        "expected": code,
                        "answer": message.content,
                        "runTrustLevel": run.trust_level,
                        "runPseudonymization": run.pseudonymization_enabled,
                        "titleGenerationStatus": session.title_generation_status if turn == 1 else None,
                        "inputTokens": input_tokens,
                        "outputTokens": output_tokens,
                        "cachedInputTokens": cached_tokens,
                        "cacheWriteInputTokens": cache_write_tokens,
                        **cache,
                        "modelRequest": performance.get("modelRequest"),
                        "effectiveToolCount": performance.get("effectiveToolCount"),
                        "offeredToolCount": performance.get("offeredToolCount"),
                        "estimatedInputTokens": performance.get("estimatedInputTokens"),
                        "contextPreparationMs": performance.get("contextPreparationMs"),
                        "timeToFirstSignalMs": performance.get("timeToFirstSignalMs"),
                        "providerDurationMs": performance.get("providerDurationMs"),
                        "elapsedSeconds": round(
                            (run.completed_at - run.created_at).total_seconds(), 3
                        )
                        if run.completed_at is not None
                        else 0,
                    }
                    previous = record
                    (diagnostic if replicate > self.args.runs else records).append(record)
        return records, diagnostic

    async def flow(self, replicate: int, memory_mode: str) -> list[dict[str, Any]]:
        codes = dict(zip(("A", "B", "C"), SESSION_CODES[replicate - 1], strict=True))
        sessions: dict[str, UUID] = {}
        previous: dict[str, dict[str, Any]] = {}
        records = []
        for label, turn, phase, foreign_requests in SEQUENCE:
            expected = codes[label]
            started = time.monotonic()
            title_status = None
            if turn == 1:
                content = (
                    "Dies ist ein kurzer Cache-Test. Die Sitzungskennung lautet "
                    f"{expected}. Antworte ausschließlich mit: {expected}"
                )
                session_id, run_id = await self._create_session(content, memory_mode)
                sessions[label] = session_id
            else:
                session_id = sessions[label]
                run_id = await self._send(session_id, FOLLOW_UPS[turn])
            record = await self._record(
                replicate=replicate,
                label=label,
                turn=turn,
                phase=phase,
                foreign_requests=foreign_requests,
                expected=expected,
                session_id=session_id,
                run_id=run_id,
                started=started,
                previous=previous.get(label),
                title_status=None,
            )
            if turn == 1:
                title_status = await self._wait_title(session_id)
                record["titleGenerationStatus"] = title_status
                record["elapsedSecondsWithTitle"] = round(time.monotonic() - started, 3)
            previous[label] = record
            records.append(record)
            print(
                f"PASS={record['passed']} rep={replicate} step={record['step']} "
                f"cache={record['cachedInputTokens']}/"
                f"{record['cacheWriteInputTokens'] if record['cacheWriteInputTokens'] is not None else '-'}"
                f"/{record['inputTokens']} "
                f"coverage={record['reusablePrefixCoverage']} "
                f"firstSignalMs={record['timeToFirstSignalMs']}",
                flush=True,
            )
        return records

    async def isolation(self) -> list[dict[str, Any]]:
        code = "K999983"
        records = []
        previous = None
        started = time.monotonic()
        session_id, run_id = await self._create_session(
            "Dies ist ein kurzer Cache-Test. Die Sitzungskennung lautet "
            f"{code}. Antworte ausschließlich mit: {code}",
            self.args.memory_mode,
        )
        first = await self._record(
            replicate=1,
            label="S",
            turn=1,
            phase="new_session",
            foreign_requests=0,
            expected=code,
            session_id=session_id,
            run_id=run_id,
            started=started,
            previous=None,
            title_status=None,
        )
        first["titleGenerationStatus"] = await self._wait_title(session_id)
        first["elapsedSecondsWithTitle"] = round(time.monotonic() - started, 3)
        records.append(first)
        previous = first
        for turn, prompt in (
            (2, FOLLOW_UPS[2]),
            (3, "Wiederhole nochmals ausschließlich dieselbe Sitzungskennung."),
        ):
            started = time.monotonic()
            run_id = await self._send(session_id, prompt)
            record = await self._record(
                replicate=1,
                label="S",
                turn=turn,
                phase="same_session_followup",
                foreign_requests=0,
                expected=code,
                session_id=session_id,
                run_id=run_id,
                started=started,
                previous=previous,
                title_status=None,
            )
            records.append(record)
            previous = record
            print(
                f"PASS={record['passed']} step={record['step']} "
                f"cache={record['cachedInputTokens']}/"
                f"{record['cacheWriteInputTokens'] if record['cacheWriteInputTokens'] is not None else '-'}"
                f"/{record['inputTokens']} "
                f"coverage={record['reusablePrefixCoverage']} "
                f"firstSignalMs={record['timeToFirstSignalMs']}",
                flush=True,
            )
        return records

    async def cleanup(self) -> list[str]:
        failures = []
        if self.client is not None:
            for session_id in reversed(self.session_ids):
                try:
                    await self._json("DELETE", f"/sessions/{session_id}")
                except Exception as exc:
                    failures.append(f"{session_id}: {type(exc).__name__}")
            try:
                await self._json("POST", "/auth/logout")
                self.token = None
            except Exception:
                pass
            await self.client.aclose()
        if self.token is not None:
            async with AsyncSessionFactory() as db:
                await remove_login_session(db, self.token)
                await db.commit()
            self.token = None
        return failures


async def run(args: argparse.Namespace) -> int:
    evaluator = SessionCacheEvaluator(args)
    started = time.monotonic()
    cleanup_failures: list[str] = []
    payload: dict[str, Any] | None = None
    try:
        await evaluator.prepare()
        if args.recover:
            records, diagnostic_records = await evaluator.recover()
        elif args.isolation:
            records = await evaluator.isolation()
            diagnostic_records = []
        else:
            await evaluator.warmup(args.memory_mode)
            records = []
            for replicate in range(1, args.runs + 1):
                records.extend(await evaluator.flow(replicate, args.memory_mode))
            diagnostic_records = []
        summary = _summary(records)
        diagnostic = None
        if diagnostic_records:
            diagnostic = {
                "memoryMode": "disabled",
                "records": diagnostic_records,
                "summary": _summary(diagnostic_records),
            }
        elif (
            summary["decision"] == "investigate"
            and args.diagnostic
            and not args.recover
            and args.memory_mode == "full"
        ):
            diagnostic_records = await evaluator.flow(args.runs + 1, "disabled")
            diagnostic = {
                "memoryMode": "disabled",
                "records": diagnostic_records,
                "summary": _summary(diagnostic_records),
            }
        payload = {
            "generatedAt": datetime.now(UTC).isoformat(),
            "model": evaluator.model_metadata,
            "runs": args.runs,
            "memoryMode": args.memory_mode,
            "sequence": (
                ["S1", "S2", "S3"]
                if args.isolation
                else [f"{label}{turn}" for label, turn, _phase, _foreign in SEQUENCE]
            ),
            "elapsedSeconds": round(time.monotonic() - started, 3),
            "recordedAgentRunSeconds": round(
                sum(record["elapsedSeconds"] for record in records), 3
            ),
            "records": records,
            "summary": summary,
            "diagnostic": diagnostic,
        }
    finally:
        cleanup_failures = await evaluator.cleanup()
        await engine.dispose()
    if payload is None:
        return 1
    payload["cleanupFailures"] = cleanup_failures
    output = Path(args.output)
    report = Path(args.report) if args.report else output.with_suffix(".md")
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(_report(payload), encoding="utf-8")
    print(
        f"\n{payload['summary']['passed']}/{payload['summary']['requests']} passed; "
        f"decision={payload['summary']['decision']}; cleanupFailures={len(cleanup_failures)}\n"
        f"Results: {output}; report: {report}"
    )
    return int(bool(cleanup_failures))


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Evaluate provider caching across real MatoLoop sessions.")
    value.add_argument("--api-base", default="http://backend:6020/api")
    value.add_argument(
        "--adapter", choices=("lemonade", "openai-compatible", "openai-codex"), default="lemonade"
    )
    value.add_argument("--model")
    value.add_argument(
        "--reasoning-effort", choices=tuple(effort.value for effort in ReasoningEffort)
    )
    value.add_argument("--user-id")
    value.add_argument("--runs", type=int, default=3, choices=range(1, 4))
    value.add_argument("--run-timeout", type=float, default=120)
    value.add_argument("--title-timeout", type=float, default=60)
    value.add_argument("--diagnostic", action=argparse.BooleanOptionalAction, default=False)
    value.add_argument("--recover", action="store_true")
    value.add_argument("--isolation", action="store_true")
    value.add_argument("--memory-mode", choices=("full", "disabled"), default="full")
    value.add_argument("--output", default="/workspace/data/session-cache-flow-qwen36-3x.json")
    value.add_argument("--report")
    return value


def main() -> None:
    argument_parser = parser()
    args = argument_parser.parse_args()
    if not args.model and args.adapter not in DEFAULT_MODELS:
        argument_parser.error("--model is required for --adapter openai-compatible")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
