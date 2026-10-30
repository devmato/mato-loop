from argparse import Namespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from mato_loop_backend import session_cache_eval
from mato_loop_backend.models import UserAccount
from mato_loop_backend.session_cache_eval import (
    SessionCacheEvaluator,
    _report,
    _summary,
    parser,
)


def test_parser_accepts_codex_with_model_default() -> None:
    args = parser().parse_args(["--adapter", "openai-codex"])

    assert args.adapter == "openai-codex"
    assert args.model is None
    assert args.reasoning_effort is None


def test_openai_compatible_requires_explicit_model(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["session-cache", "--adapter", "openai-compatible"])

    with pytest.raises(SystemExit):
        session_cache_eval.main()

    assert "--model is required" in capsys.readouterr().err


async def test_session_payload_omits_unspecified_reasoning_effort() -> None:
    evaluator = SessionCacheEvaluator(Namespace())
    evaluator.model_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    evaluator._json = AsyncMock(
        side_effect=[
            {
                "webSearchPolicy": "available",
                "preferredExecutionAgentId": None,
                "currentDirectory": None,
            },
            {"session": {"id": str(session_id)}, "runId": str(run_id)},
        ]
    )

    assert await evaluator._create_session("Test", "full") == (session_id, run_id)
    payload = evaluator._json.await_args_list[1].kwargs["payload"]

    assert "reasoning_effort" not in payload


async def test_prepare_uses_general_profile_for_ai_defaults(monkeypatch) -> None:
    user = UserAccount(id=uuid4(), username="developer", is_active=True)
    db = AsyncMock()
    users = MagicMock()
    users.all.return_value = [user]
    db.scalars.return_value = users
    db.scalar.return_value = 0

    @asynccontextmanager
    async def session_factory():
        yield db

    profile_ai = AsyncMock(side_effect=RuntimeError("stop after profile lookup"))
    monkeypatch.setattr(session_cache_eval, "AsyncSessionFactory", session_factory)
    monkeypatch.setattr(session_cache_eval, "load_effective_ai", profile_ai)
    evaluator = SessionCacheEvaluator(Namespace(user_id=None, recover=False))

    try:
        await evaluator.prepare()
    except RuntimeError as exc:
        assert str(exc) == "stop after profile lookup"

    assert profile_ai.await_args.args[1:] == (user.id,)


def test_report_includes_cache_reads_and_writes() -> None:
    record = {
        "replicate": 1,
        "step": "A1",
        "phase": "new_session",
        "passed": True,
        "inputTokens": 1_200,
        "outputTokens": 10,
        "cachedInputTokens": 0,
        "cacheWriteInputTokens": 1_024,
        "rawCacheHitRate": 0.0,
        "reusablePrefixCoverage": None,
        "avoidableReprocessedTokens": None,
        "timeToFirstSignalMs": 100,
        "providerDurationMs": 200,
        "elapsedSeconds": 0.2,
    }
    summary = _summary([record])
    payload = {
        "model": {
            "modelId": "openai-codex/gpt-5.6-sol",
            "providerAdapter": "openai-codex",
            "trustLevel": "public",
            "pseudonymization": False,
            "reasoningEffort": "model_default",
            "titleModelId": None,
        },
        "sequence": ["A1"],
        "elapsedSeconds": 0.2,
        "recordedAgentRunSeconds": 0.2,
        "records": [record],
        "summary": summary,
        "diagnostic": None,
    }
    report = _report(payload)

    assert summary["phases"]["new_session"]["cacheWriteInputTokens"]["median"] == 1_024
    assert "Prompt cache key: `chat session ID`" in report
    assert "0 / 1024 / 1200" in report

    payload["model"]["providerAdapter"] = "lemonade"
    assert "Prompt cache key: `not sent (provider-managed cache)`" in _report(payload)
