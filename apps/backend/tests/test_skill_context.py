import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from context_helpers import _build_context
from runtime_helpers import _runtime_setup
from tool_helpers import _context

import mato_loop_backend.agent.loop as agent_loop
from mato_loop_backend.agent.context import ContextManager
from mato_loop_backend.agent.projection import _projection_messages
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.agent.tool_round import persist_tool_result
from mato_loop_backend.files.service import RootFileService
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.models import AgentRun, ContextSummary, Message, WebSearchPolicy
from mato_loop_backend.permissions.policy import PermissionDecision
from mato_loop_backend.providers import openai_compatible as provider_module
from mato_loop_backend.providers.base import ProviderResult, ToolCall
from mato_loop_backend.providers.service import ProviderService
from mato_loop_backend.settings.models import (
    AgentRuntimeSettings,
    InstanceLimits,
    LlmProviderConfig,
    ModelConfig,
    SkillDefinition,
    TrustLevel,
)
from mato_loop_backend.skills import SkillService
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.tools.sessions import _message_item

build_context = ContextManager.build


@pytest.mark.parametrize("change", [None, "revision", "owner", "disabled", "approval", "file", "read-denied", "private"])
async def test_followup_reuses_only_current_authorized_skill_context(monkeypatch, tmp_path, skill_library, change):
    skill = SkillDefinition(name="team-updates", description="Draft team updates.", instructions="Open with the team name.")
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch, [ProviderResult(content="Team: New update.")] * (2 if change is None else 1),
        skills=[skill], search_policy=WebSearchPolicy.DISABLED,
    )
    session = db.get.return_value
    session.session_type = "chat"
    monkeypatch.setattr(ContextManager, "build", build_context)
    records = AgentRuntime._records.return_value
    user = records[0]
    user.created_at = datetime.now(UTC)
    user.content = "Make the team update shorter."
    run_id = uuid4()
    base = f"skills/{run_id}/{uuid4()}/{skill.name}"
    resource = "references/tone.md"
    path = f"{base}/{resource}"
    text = "Short, friendly example."
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    files = RootFileService(session.id, session_storage=storage)
    for directory in ("skills", f"skills/{run_id}", base.rsplit("/", 1)[0], base, f"{base}/references"):
        files.create_directory("session", directory)
    files.write_file("session", path, "Changed example." if change == "file" else text)
    assert files.working_revision(path).sha256 == hashlib.sha256(
        ("Changed example." if change == "file" else text).encode()
    ).hexdigest()
    monkeypatch.setattr(agent_loop, "RootFileService", lambda *_args, **_kwargs: files)

    def result(name, content, metadata, seconds, trust="public"):
        return Message(
            id=uuid4(), role="tool", session_id=session.id, content=content, trust_level=trust,
            tool_call_id=name,
            created_at=user.created_at - timedelta(seconds=seconds),
            provider_data={"_matoloop": {"runId": str(run_id), "toolName": name, **metadata}},
        )

    results = [
        result("load_skill", json.dumps({"id": str(skill.id), "loaded": True, "revision": skill.revision,
                                         "filesBasePath": base, "computerBasePath": "/obsolete/lease"}),
               {"skillLoad": {"ownerId": str(session.user_id)}}, 2, "private" if change == "private" else "public"),
        result("read", f"1: {text}", {"skillReferenceRead": {
            "path": path, "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }}, 1),
    ]
    original_user = Message(id=uuid4(), session_id=session.id, role="user", content="Write a team update.",
                            created_at=user.created_at - timedelta(seconds=3))
    history = [original_user]
    for result_row in results:
        history.extend((Message(id=uuid4(), role="assistant", session_id=session.id, content="",
                                created_at=result_row.created_at, provider_data={"_matoloop": {"runId": str(run_id)}},
                                tool_calls=[{"id": result_row.tool_call_id, "name": result_row.tool_call_id, "arguments": {}}]),
                        result_row))
    records[:0] = history
    catalog = await SkillService.list(db, session.user_id)
    definition = next(item for item in catalog if item.id == skill.id)
    definition.resource_paths.append(resource)
    monkeypatch.setattr(SkillService, "list", AsyncMock(return_value=catalog))
    current, allowed = await SkillService.effective_references(db, session.user_id)
    if change == "revision":
        current = [(ref.model_copy(update={"revision": "a" * 64}), owner) for ref, owner in current]
    elif change == "owner":
        current = [(ref, None) for ref, _ in current]
    elif change == "disabled":
        current = []
    monkeypatch.setattr(SkillService, "effective_references", AsyncMock(return_value=(current, allowed)))

    async def authorize(_db, _run, _session, call, _request, *, root_files, check_only):
        assert check_only and root_files is files
        if change == "approval" and call.name == "load_skill":
            return PermissionDecision.ASK
        if change == "read-denied" and call.name == "read":
            return PermissionDecision.DENY
        return PermissionDecision.ALLOW

    monkeypatch.setattr(AgentRuntime, "_authorize", authorize)
    await AgentRuntime._execute_loop(db, run)

    assert len(requests) == 1
    tool_registry._execute_definition.assert_not_awaited()
    reused = change in {None, "file", "read-denied"}
    contents = [message["content"] for message in requests[0]["messages"]]
    assert skill.instructions not in contents[0]
    assert (skill.instructions in "\n".join(contents)) is reused
    assert (text in "\n".join(contents)) is (change is None)
    assert "/obsolete/lease" not in "\n".join(contents)
    assert skill.instructions not in results[0].content

    if change is None:
        for item in records:
            item.provider_data = json.loads(json.dumps(item.provider_data, sort_keys=True))
        next_user = Message(id=uuid4(), session_id=session.id, role="user", content="Shorter still.",
                            created_at=user.created_at + timedelta(seconds=1))
        records.append(next_user)
        run.id = uuid4()
        run.input_message_id = next_user.id
        await AgentRuntime._execute_loop(db, run)
        provider = LlmProviderConfig(name="Test", adapter_type="openai-compatible")
        model = ModelConfig(provider_id=provider.id, model_id="test")
        prepared = [await ProviderService.prepare_request(
            provider=provider, model=model, tools=request["tools"], messages=request["messages"],
            pseudonymization_enabled=False,
        ) for request in requests]
        for encode, key in ((provider_module._chat_body, "messages"), (provider_module._responses_body, "input")):
            first, second = (encode(item.request) for item in prepared)
            # The request-local tool-budget reminder is not retained history.
            retained = first[key][:-1] if "Host runtime state" in str(first[key][-1]) else first[key]
            assert second[key][:len(retained)] == retained
            assert second["tools"] == first["tools"]

        summary = ContextSummary(covered_through_message_id=results[-1].id, content="Wrote an update.")
        db.scalar.side_effect = None
        db.scalar.return_value = summary
        read_bytes = AsyncMock(side_effect=AssertionError("Compacted reference should not be read"))
        monkeypatch.setattr(files, "read_bytes", read_bytes)
        compacted = await _build_context(
            db, chat_session=session, records=records, system_prompt=contents[0], tool_schemas=[],
            model=model, settings=AgentRuntimeSettings(), summarize=AsyncMock(),
            load_skill_results=lambda ids: agent_loop.prepare_skill_context(
                db, records, ids, run_id=run.id, user_id=session.user_id, root_files=files,
                state=agent_loop.RunSkillState(catalog={definition.id: definition}), trust=TrustLevel.PUBLIC,
                authorize=AsyncMock(return_value=PermissionDecision.ALLOW), can_load=True, can_read=True,
                reuse_history=True, computer_id=None,
            ),
        )
        checkpoint = next(item["content"] for item in compacted.messages if "[Host skill checkpoint]" in item["content"])
        assert skill.instructions in checkpoint and text not in checkpoint and "/obsolete/lease" not in checkpoint


async def test_large_skill_fits_or_uses_authorized_result_paging(monkeypatch, tmp_path):
    load_id = uuid4()
    call = ToolCall("page", "session_tool_result_read", {"message_id": str(load_id)})
    ctx = _context(tmp_path, call)
    skill = SkillDefinition(name="long-skill", description="Long instructions.",
                            instructions="a" * 40_000 + "Required middle rule." + "b" * 40_000,
                            revision="a" * 64, owner_id=ctx.session.user_id)
    base = f"skills/{ctx.run_id}/{uuid4()}/{skill.name}"
    for path in ("skills", f"skills/{ctx.run_id}", base.rsplit("/", 1)[0], base):
        ctx.root_files.create_directory("session", path)
    ctx.skill_state.active[skill.id] = skill
    ctx.check_permission = AsyncMock(return_value=PermissionDecision.ALLOW)
    monkeypatch.setattr(SkillService, "effective_references", AsyncMock(return_value=([(skill, skill.owner_id)], True)))
    host = {"runId": str(ctx.run_id)}
    origin = Message(id=uuid4(), session_id=ctx.session.id, role="assistant", content="",
                     tool_calls=[{"id": "load", "name": "load_skill", "arguments": {}}],
                     provider_data={"_matoloop": host})
    load = Message(id=load_id, session_id=ctx.session.id, role="tool", tool_call_id="load", trust_level="public",
                   content=json.dumps({"id": str(skill.id), "loaded": True, "revision": skill.revision,
                                       "filesBasePath": base}), provider_data={"_matoloop": {
                       **host, "toolName": "load_skill", "skillLoad": {"ownerId": str(skill.owner_id)},
                   }})
    user = Message(id=uuid4(), session_id=ctx.session.id, role="user", content="Use the skill.")
    records = [user, origin, load]
    results = await agent_loop.prepare_skill_context(
        ctx.db, records, run_id=ctx.run_id, user_id=ctx.session.user_id, root_files=ctx.root_files,
        state=ctx.skill_state, trust=TrustLevel.PUBLIC, authorize=ctx.check_permission,
        can_load=True, can_read=True, reuse_history=True, computer_id=None,
    )
    full = _projection_messages("System", None, records, TrustLevel.PUBLIC, skill_results=results)
    assert json.loads(full[-1]["content"])["authorizedSkill"]["instructions"] == skill.instructions
    limited = _projection_messages("System", None, records, TrustLevel.PUBLIC, skill_results=results,
                                   current_turn_tool_output_tokens=100)
    fallback = json.loads(limited[-1]["content"])["toolResultReference"]
    ctx.db.scalar.return_value = load
    ctx.db.scalars.return_value = Mock()
    ctx.db.scalars.return_value.all.return_value = records
    call.arguments.update(fallback["arguments"], offset=36_000)
    page = await tool_registry.execute(call, ctx)
    assert "Required middle rule." in json.loads(page.content)["content"]
    reader = Message(id=uuid4(), session_id=ctx.session.id, role="assistant", content="",
                     tool_calls=[{"id": call.id, "name": call.name, "arguments": call.arguments}],
                     provider_data={"_matoloop": host})
    recorded, _, _ = await persist_tool_result(
        ctx.db, AgentRun(id=ctx.run_id, session_id=ctx.session.id), ctx.session, call,
        content=page.content, metadata=page.metadata, attachments=(), file_actions=(),
        trust_level=TrustLevel.PUBLIC, ephemeral_output=False, limits=InstanceLimits(), attachment_service=Mock(),
    )
    recorded.id = uuid4()
    assert json.loads(recorded.content)["content"] == ""
    replay = _projection_messages("System", None, [*records, reader, recorded], TrustLevel.PUBLIC, skill_results=results)
    assert json.loads(replay[-1]["content"]) == json.loads(page.content)
    ctx.check_permission.return_value = PermissionDecision.DENY
    assert (await tool_registry.execute(call, ctx)).metadata["errorCode"] == "permission.denied"


async def test_session_history_does_not_return_stale_skill_reference(tmp_path):
    call = ToolCall("page", "session_tool_result_read", {"message_id": str(uuid4())})
    ctx = _context(tmp_path, call)
    tool = Message(id=uuid4(), role="tool", session_id=ctx.session.id, tool_call_id="read", trust_level="public",
                   content="Formerly authorized reference.", provider_data={"_matoloop": {
                       "toolName": "read", "skillReferenceRead": {"path": "stale", "sha256": "stale"},
                   }})
    origin = Message(id=uuid4(), role="assistant", session_id=ctx.session.id, content="",
                     tool_calls=[{"id": "read", "name": "read", "arguments": {}}])
    direct, _ = _message_item(tool, 5000, {tool.id: "read"}, TrustLevel.PUBLIC,
                              reference_tool_results=True, available_secret_references=set())
    assert tool.content not in direct["content"]
    call.arguments = direct["toolResultReference"]["arguments"]
    ctx.db.scalar.return_value = tool
    ctx.db.scalars.return_value = Mock()
    ctx.db.scalars.return_value.all.return_value = [origin, tool]
    ctx.check_permission = AsyncMock(return_value=PermissionDecision.ALLOW)
    assert (await tool_registry.execute(call, ctx)).metadata["errorCode"] == "permission.denied"
