import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import aiohttp
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from mato_loop_backend.agent.run_contracts import AgentRunError
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.db.base import Base
from mato_loop_backend.models import (
    AgentRun,
    ChatSession,
    ContextSummary,
    Message,
    UserAccount,
    VoiceConnection,
    VoiceDelegation,
)
from mato_loop_backend.providers.realtime import transport
from mato_loop_backend.security import store_credential
from mato_loop_backend.sessions.service import SessionServiceError
from mato_loop_backend.settings import SettingsStore
from mato_loop_backend.settings.models import (
    AgentProfile,
    AiProviderSettings,
    LlmProviderConfig,
    ModelCapability,
    ModelConfig,
    ModelDefaultsSettings,
    TrustLevel,
)
from mato_loop_backend.voice import delegation
from mato_loop_backend.voice import service as voice


@pytest.fixture
async def voice_db(request, monkeypatch, tmp_path):
    if not request.config.getoption("--voice-db"):
        pytest.skip("requires the development PostgreSQL database")
    config = get_runtime_config()
    schema = f"voice_test_{uuid4().hex}"
    admin = create_async_engine(config.database_url, poolclass=NullPool)
    engine = create_async_engine(config.database_url, poolclass=NullPool, connect_args={"server_settings": {"search_path": f"{schema},public"}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(voice, "AsyncSessionFactory", factory)
    monkeypatch.setattr(config, "data_dir", tmp_path)
    monkeypatch.setattr(config, "embedding_enabled", False)
    monkeypatch.setattr(AgentRuntime, "_schedule_run", Mock())
    coordinator = voice.VoiceCoordinator()
    socket = SimpleNamespace(closed=False, send_json=AsyncMock(), receive=AsyncMock())
    queue = asyncio.Queue()
    socket.receive.side_effect = queue.get

    @asynccontextmanager
    async def connect(*_args, **_kwargs):
        yield socket

    monkeypatch.setattr(transport, "connect_controller", connect)
    monkeypatch.setattr(transport, "negotiate", AsyncMock(return_value=transport.RealtimeCall("rtc_test", "v=0\r\nanswer")))
    monkeypatch.setattr(transport, "hangup", AsyncMock())
    async with admin.begin() as db:
        await db.execute(CreateSchema(schema))
    try:
        async with engine.begin() as db:
            await db.run_sync(lambda connection: Base.metadata.create_all(connection, checkfirst=False))
        async with factory() as db:
            user = UserAccount(username="voice-test", is_admin=True)
            db.add(user)
            await db.flush()
            credential = await store_credential(db, owner_id=None, kind="azure-openai-realtime-api-key", label="Test", payload={"api_key": "test-key"})
            provider = LlmProviderConfig(name="Voice", adapter_type="azure-openai-realtime", credential_id=credential.id,
                                         config={"base_url": "https://voice.openai.azure.com/openai/v1"}, pseudonymization_enabled=False)
            model = ModelConfig(provider_id=provider.id, model_id="voice-deployment", capabilities={ModelCapability.REALTIME_AUDIO})
            worker_provider = LlmProviderConfig(name="Worker", adapter_type="openai-compatible", pseudonymization_enabled=False, config={"base_url": "https://worker.example/v1"})
            worker = ModelConfig(provider_id=worker_provider.id, model_id="worker")
            await SettingsStore.save(db, AiProviderSettings(providers=[provider, worker_provider], models=[model, worker]))
            await SettingsStore.save(db, ModelDefaultsSettings(default_model_id=worker.id, realtime_model_id=model.id))
            session = ChatSession(user_id=user.id, title="Voice test", memory_mode="disabled")
            db.add(session)
            await db.commit()
        yield SimpleNamespace(factory=factory, coordinator=coordinator, user=user, session=session, model=model, queue=queue, socket=socket)
    finally:
        await coordinator.shutdown()
        await engine.dispose()
        async with admin.begin() as db:
            await db.execute(DropSchema(schema, cascade=True))
        await admin.dispose()


async def begin(tree):
    async with tree.factory() as db:
        result = await tree.coordinator.start(db, tree.session.id, tree.user.id, "v=0\r\n", tree.model.id)
    live = next(iter(tree.coordinator.live.values()))
    await tree.queue.put(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps({
        "type": "session.updated", "session": {"audio": {"input": {"turn_detection": {"type": "server_vad"}}}, "output_modalities": ["text"], "tools": [delegation.TASK_TOOL]},
    })))
    for _ in range(100):
        if live.phase != "starting":
            break
        await asyncio.sleep(.01)
    assert live.phase == "listening", live.error_code
    return live, result


async def test_voice_admission_transcript_deduplication_and_barge_in(voice_db):
    tree = voice_db
    live, _ = await begin(tree)
    async with tree.factory() as db:
        with pytest.raises(SessionServiceError, match="voice.busy"):
            await tree.coordinator.start(db, tree.session.id, tree.user.id, "v=0\r\n", tree.model.id)
        with pytest.raises(AgentRunError, match="voice.busy"):
            await AgentRuntime.start(db, tree.session, "Text cannot start concurrently.")
        with pytest.raises(AgentRunError, match="voice.busy"):
            await AgentRuntime.start(db, tree.session, "No reserved task.", voice_connection_id=live.id)
    await tree.coordinator.event(live, {"type": "input_audio_buffer.speech_started", "item_id": "user-1"})
    await tree.coordinator.event(live, {"type": "response.created", "response": {"metadata": {"matoloop_phase": "speech"}}})
    await tree.coordinator.event(live, {"type": "response.output_item.added", "item": {"id": "assistant-1", "type": "message", "role": "assistant"}})
    await tree.coordinator.event(live, {"type": "response.output_audio_transcript.done", "item_id": "assistant-1", "transcript": "Generated reply"})
    await tree.coordinator.event(live, {"type": "conversation.item.input_audio_transcription.completed", "item_id": "user-1", "transcript": "Hello"})
    await tree.coordinator.event(live, {"type": "conversation.item.input_audio_transcription.completed", "item_id": "user-1", "transcript": "Duplicate"})
    live.phase = "speaking"
    await tree.coordinator.event(live, {"type": "input_audio_buffer.speech_started", "item_id": "user-2"})
    async with tree.factory() as db:
        messages = (await db.scalars(select(Message).where(Message.session_id == tree.session.id).order_by(Message.created_at, Message.id))).all()
        assert [(message.role, message.content, message.generation_status) for message in messages] == [
            ("user", "Hello", "completed"), ("assistant", "Generated reply", "interrupted"), ("user", "", "interrupted"),
        ]
    await tree.coordinator.event(live, {"type": "response.done", "response": {"status": "cancelled"}})
    await tree.coordinator.event(live, {"type": "response.created", "response": {"metadata": {"matoloop_phase": "speech"}}})
    await tree.coordinator.event(live, {"type": "response.output_item.added", "item": {"id": "assistant-2", "type": "message", "role": "assistant"}})
    await tree.coordinator.event(live, {"type": "response.output_audio_transcript.done", "item_id": "assistant-2", "transcript": "Heard reply"})
    await tree.coordinator.event(live, {"type": "response.done", "response": {"status": "completed", "output": []}})
    await tree.coordinator.event(live, {"type": "output_audio_buffer.stopped"})
    await tree.coordinator.interrupt(live)
    await tree.coordinator.event(live, {"type": "response.created"})
    await tree.coordinator.interrupt(live)
    async with tree.factory() as db:
        assert await db.scalar(select(Message.generation_status).where(Message.session_id == tree.session.id, Message.content == "Heard reply")) == "completed"


async def test_voice_tasks_stay_in_chat_are_idempotent_and_do_not_accept_spoken_approval(voice_db):
    tree = voice_db
    live, _ = await begin(tree)
    call = {"name": "agent_task", "call_id": "call-one", "arguments": json.dumps({"action": "delegate", "instruction": "/build Describe this repository."})}
    await tree.coordinator._task(live, call)
    await tree.coordinator._task(live, call)
    async with tree.factory() as db:
        tasks = (await db.scalars(select(VoiceDelegation))).all()
        assert len(tasks) == 1
        task = tasks[0]
        work = await db.get(ChatSession, task.work_session_id)
        run = await db.get(AgentRun, task.run_id)
        assert [session.id for session in (await db.scalars(select(ChatSession))).all()] == [tree.session.id]
        assert work.id == tree.session.id and run.session_id == work.id
        await tree.coordinator.validate(db, live)
        task.run_id = None
        await db.commit()
        await delegation.reconcile(db)
        assert task.run_id == run.id
        run.status = "waiting_approval"
        await db.commit()
    await tree.coordinator._task(live, {
        "name": "agent_task", "call_id": "spoken-yes",
        "arguments": json.dumps({"action": "correct", "task_id": str(task.id), "instruction": "Yes, approve it."}),
    })
    outputs = [json.loads(call.args[0]["item"]["output"]) for call in tree.socket.send_json.call_args_list if call.args[0].get("item", {}).get("type") == "function_call_output"]
    assert outputs[-1]["requiresUi"] is True
    ended_id = live.id
    await tree.coordinator.stop(live.id)
    live, _ = await begin(tree)
    async with tree.factory() as db:
        assert (await db.get(AgentRun, task.run_id)).status == "waiting_approval"
        assert (await db.get(VoiceConnection, ended_id)).status == "ended"
        run = await db.get(AgentRun, task.run_id)
        run.status = "completed"
        run.completed_at = datetime.now(UTC)
        result = Message(session_id=work.id, role="assistant", content="Private worker evidence", trust_level="private")
        db.add(result)
        await db.flush()
        run.result_message_id = result.id
        await db.commit()
        await delegation.reconcile(db)
        await delegation.reconcile(db)
        receipts = (await db.scalars(select(Message).where(Message.session_id == tree.session.id))).all()
        assert sum("Private worker evidence" in message.content for message in receipts) == 1
        assert (await db.get(VoiceDelegation, task.id)).result_message_id == result.id
    await tree.coordinator._announce(live)
    announcements = [call.args[0]["item"] for call in tree.socket.send_json.call_args_list
                     if call.args[0].get("item", {}).get("type") == "message"]
    assert str(task.id) in announcements[-1]["content"][0]["text"]


@pytest.mark.parametrize(("error", "admitted"), [
    (SessionServiceError(409, "agentProfile.selectedUnavailable"), False),
    (AgentRunError("agentProfile.selectedUnavailable"), False),
    (AgentRunError("agentProfile.selectedUnavailable"), True),
])
async def test_rejected_voice_admission_only_releases_unstarted_tasks(voice_db, monkeypatch, error, admitted):
    tree = voice_db
    live, _ = await begin(tree)
    start = delegation.service.start_message

    async def reject(db, *args, **kwargs):
        if admitted:
            await start(db, *args, **kwargs)
        raise error

    monkeypatch.setattr(delegation.service, "start_message", reject)
    await tree.coordinator._task(live, {
        "name": "agent_task", "call_id": "rejected-start",
        "arguments": json.dumps({"action": "delegate", "instruction": "Search the web."}),
    })
    monkeypatch.setattr(delegation.service, "start_message", start)
    await tree.coordinator._task(live, {
        "name": "agent_task", "call_id": "retry-start",
        "arguments": json.dumps({"action": "delegate", "instruction": "Search the web."}),
    })
    output = json.loads(tree.socket.send_json.call_args.args[0]["item"]["output"])
    assert output.get("error", {}).get("code") == ("voice.taskBusy" if admitted else None)


async def test_voice_correction_rechecks_ui_wait_after_revalidation(voice_db):
    tree = voice_db
    live, _ = await begin(tree)
    await tree.coordinator._task(live, {
        "name": "agent_task", "call_id": "start",
        "arguments": json.dumps({"action": "delegate", "instruction": "Inspect the repository."}),
    })
    async with tree.factory() as db:
        task = await db.scalar(select(VoiceDelegation))
        run = await db.get(AgentRun, task.run_id)
        run.status = "running"
        await db.commit()

        async def revalidate(_db):
            async with tree.factory() as other:
                current = await other.get(AgentRun, run.id)
                current.status = "waiting_input"
                await other.commit()

        result = await delegation.perform(
            db, await db.get(VoiceConnection, live.id), "correction",
            delegation.TaskArguments(action="correct", task_id=str(task.id), instruction="Yes, do that."),
            revalidate=revalidate,
        )
        assert result.get("requiresUi") is True


async def test_unknown_voice_dispatch_never_adopts_an_older_chat_run(voice_db):
    tree = voice_db
    async with tree.factory() as db:
        old = AgentRun(session_id=tree.session.id, status="completed")
        task = VoiceDelegation(
            user_id=tree.user.id, source_session_id=tree.session.id,
            work_session_id=tree.session.id, created_at=datetime.now(UTC) - timedelta(seconds=91),
        )
        db.add_all([old, task])
        await db.commit()
        await delegation.reconcile(db)
        assert (task.status, task.run_id) == ("outcome_unknown", None)


@pytest.mark.parametrize(("trust", "protected", "expected"), [
    ("public", False, {"result": "Fresh search finding"}),
    ("public", True, {"result": "<protected finding>"}),
    ("private", False, {"resultAvailableInUi": True}),
])
async def test_completed_status_delivers_the_answer_before_reconciliation(voice_db, trust, protected, expected):
    tree = voice_db
    live, _ = await begin(tree)
    if protected:
        live.protector = SimpleNamespace(
            restore_value=lambda value: value,
            protect_text=lambda text: text.replace("Fresh search finding", "<protected finding>"),
        )
    async with tree.factory() as db:
        message = Message(session_id=tree.session.id, role="assistant", content="Fresh search finding", trust_level=trust)
        db.add(message)
        await db.flush()
        run = AgentRun(session_id=tree.session.id, status="completed", result_message_id=message.id, completed_at=datetime.now(UTC))
        db.add(run)
        await db.flush()
        task = VoiceDelegation(
            user_id=tree.user.id, source_session_id=tree.session.id,
            work_session_id=tree.session.id, run_id=run.id, status="running",
        )
        db.add(task)
        await db.commit()
    await tree.coordinator._task(live, {
        "name": "agent_task", "call_id": "completed-status",
        "arguments": json.dumps({"action": "status", "task_id": str(task.id)}),
    })
    output = json.loads(tree.socket.send_json.call_args.args[0]["item"]["output"])
    assert {key: output[key] for key in ("result", "resultAvailableInUi") if key in output} == expected
    async with tree.factory() as db:
        await delegation.reconcile(db)
    sent = tree.socket.send_json.call_count
    await tree.coordinator._announce(live)
    assert tree.socket.send_json.call_count == sent


async def test_cancelled_responses_never_dispatch_partial_function_calls():
    coordinator = voice.VoiceCoordinator()
    coordinator._mark_interrupted = AsyncMock()
    coordinator._task = AsyncMock()
    live = SimpleNamespace(response_active=True, output_item=None)
    await coordinator.event(live, {"type": "response.output_item.done", "item": {
        "type": "function_call", "status": "incomplete", "name": "agent_task", "arguments": "{}",
    }})
    await coordinator.event(live, {"type": "response.done", "response": {
        "status": "cancelled", "output": [{"type": "function_call", "name": "agent_task", "arguments": "{}"}],
    }})
    coordinator._task.assert_not_called()


async def test_closing_during_negotiation_hangs_up_the_late_call(voice_db, monkeypatch):
    tree = voice_db
    entered, release = asyncio.Event(), asyncio.Event()

    async def negotiate(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return transport.RealtimeCall("rtc_late", "v=0\r\n")

    monkeypatch.setattr(transport, "negotiate", negotiate)

    async def start():
        async with tree.factory() as db:
            return await tree.coordinator.start(db, tree.session.id, tree.user.id, "v=0\r\n", tree.model.id)

    pending = asyncio.create_task(start())
    await asyncio.wait_for(entered.wait(), 2)
    await tree.coordinator.stop(next(iter(tree.coordinator.live)))
    release.set()
    with pytest.raises(SessionServiceError, match="voice.notActive"):
        await pending
    assert transport.hangup.await_args.kwargs == {"api_key": "test-key", "call_id": "rtc_late"}


async def test_disabling_a_live_model_revokes_the_binding(voice_db):
    tree = voice_db
    live, _ = await begin(tree)
    async with tree.factory() as db:
        settings = await SettingsStore.load(db, AiProviderSettings)
        next(model for model in settings.models if model.id == tree.model.id).enabled = False
        await SettingsStore.save(db, settings)
        await db.commit()
        with pytest.raises(SessionServiceError, match="voice.unavailable"):
            await tree.coordinator.validate(db, live)
    await tree.coordinator.stop(live.id, error_code="voice.unavailable")
    async with tree.factory() as db:
        assert (await db.get(VoiceConnection, live.id)).error_code == "voice.unavailable"


async def test_slow_task_dispatch_does_not_block_speech_events():
    coordinator = voice.VoiceCoordinator()
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def task(*_args):
        entered.set()
        await release.wait()
        completed.set()

    coordinator._task = AsyncMock(side_effect=task)
    coordinator._interrupt = AsyncMock()
    coordinator._item = AsyncMock()
    live = SimpleNamespace(
        response_active=True, response_is_speech=False, tool_response_pending=False, queued_call_ids=set(),
        batches=asyncio.Queue(), task_lock=asyncio.Lock(), user_speaking=False,
        phase="thinking", instructions="", send=AsyncMock(),
    )
    call = {"type": "function_call", "status": "completed", "name": "agent_task", "call_id": "early-call", "arguments": "{}"}
    await coordinator.event(live, {"type": "response.output_item.done", "item": call})
    dispatch = asyncio.create_task(coordinator._dispatch(live))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await coordinator.event(live, {"type": "input_audio_buffer.speech_started", "item_id": "new-utterance"})
        coordinator._item.assert_awaited_once_with(live, "new-utterance", "user")
        await coordinator.event(live, {"type": "input_audio_buffer.speech_stopped"})
        await coordinator.event(live, {"type": "response.done", "response": {
            "status": "completed", "output": [call],
        }})
        responses_before_admission = live.send.await_count
        release.set()
        await asyncio.wait_for(completed.wait(), 1)
        speech = live.send.await_args.args[0]["response"]
        assert (coordinator._task.await_count, responses_before_admission, {
            key: speech[key] for key in ("output_modalities", "tools", "tool_choice")
        }) == (1, 0, {"output_modalities": ["audio"], "tools": [], "tool_choice": "none"})
    finally:
        dispatch.cancel()
        await asyncio.gather(dispatch, return_exceptions=True)


async def test_silent_direct_answers_are_spoken_without_persisting_the_draft():
    coordinator = voice.VoiceCoordinator()
    coordinator._item = AsyncMock()
    live = SimpleNamespace(
        response_active=True, response_is_speech=False, tool_response_pending=False,
        task_lock=asyncio.Lock(), batches=asyncio.Queue(), user_speaking=False,
        phase="thinking", instructions="", send=AsyncMock(),
    )
    draft = {"type": "message", "role": "assistant", "id": "draft", "content": [
        {"type": "output_text", "text": "Hallo!"},
    ]}
    await coordinator.event(live, {"type": "response.created", "response": {"metadata": None}})
    await coordinator.event(live, {"type": "response.output_item.added", "item": draft})
    await coordinator.event(live, {"type": "response.done", "response": {
        "status": "completed", "output": [draft],
    }})
    speech = live.send.await_args.args[0]["response"]
    await coordinator.event(live, {"type": "response.created", "response": {"metadata": speech["metadata"]}})
    await coordinator.event(live, {"type": "response.output_item.added", "item": {
        "type": "message", "role": "assistant", "id": "spoken",
    }})
    coordinator._item.assert_awaited_once_with(live, "spoken", "assistant")


async def test_public_voice_context_excludes_private_history(monkeypatch):
    from mato_loop_backend.voice import context

    db = AsyncMock()
    db.scalar.return_value = None
    db.scalars.return_value.all = Mock(return_value=[
        Message(role="user", content="Private detail", trust_level="private"),
        Message(role="user", content="Public detail", trust_level="public"),
    ])
    monkeypatch.setattr(context, "load_agent_profile", AsyncMock(return_value=AgentProfile(name="Voice", instructions="Keep restrictions.")))
    monkeypatch.setattr(context, "load_effective_agent_identity", AsyncMock(return_value=SimpleNamespace(persona=SimpleNamespace(instructions=""))))
    _, history = await context.project_context(
        db, ChatSession(id=uuid4(), user_id=uuid4()), ModelConfig(provider_id=uuid4(), model_id="voice"),
        TrustLevel.PUBLIC, None,
    )
    assert history == [{"role": "user", "content": "Public detail"}]


async def test_public_voice_omits_private_summary_and_maintained_context(voice_db):
    from mato_loop_backend.voice.context import project_context

    tree = voice_db
    async with tree.factory() as db:
        source = await db.get(ChatSession, tree.session.id)
        source.session_profile = {"context_state": {
            "revision": 1, "trust_level": "private",
            "blocks": [{"key": "notes", "text": "private-token"}],
        }}
        message = Message(session_id=source.id, role="user", content="private-token", trust_level="private")
        db.add(message)
        await db.flush()
        db.add(ContextSummary(session_id=source.id, covered_through_message_id=message.id,
                              content="private-token", estimated_tokens=10))
        await db.commit()
        instructions, history = await project_context(db, source, tree.model, TrustLevel.PUBLIC, None)
        assert "private-token" not in json.dumps([instructions, history])
