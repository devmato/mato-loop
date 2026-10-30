import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tool_helpers import _context

from mato_loop_backend.agent.projection import _target_tool_result
from mato_loop_backend.memory.episode_retrieval import EpisodeReference
from mato_loop_backend.models import ChatSession, MemoryRecord
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings import IntegrationSettings, ToolSettings, TrustLevel
from mato_loop_backend.tools import context_search as search
from mato_loop_backend.tools.catalog import tool_registry


@pytest.mark.parametrize('memory_status', ['searched', 'unavailable', 'denied'])
@pytest.mark.parametrize('include_private', [False, True])
@pytest.mark.parametrize('all_projects', [False, True])
async def test_context_search_combines_sources_without_requiring_memory(tmp_path, monkeypatch, memory_status, include_private, all_projects):
    call = ToolCall('search', 'context_search', {'query': 'Atlas address', 'include_private': include_private, 'all_projects': all_projects})
    context = _context(tmp_path, call)
    context.session.project_id = uuid4()
    context.model_trust_level = TrustLevel.PUBLIC
    context.memory_read_enabled = memory_status != 'unavailable'
    if memory_status == 'denied':
        from mato_loop_backend.permissions.policy import PermissionDecision

        async def approve(request, *, check_only=False):
            if request.capability == 'memory.read':
                return PermissionDecision.ASK if check_only else PermissionDecision.USER_DENIED
            return PermissionDecision.ALLOW

        context.ask_handler.side_effect = approve
    monkeypatch.setattr(search.access, 'profile_tool_settings', AsyncMock(return_value=context.tool_settings))
    monkeypatch.setattr(search, 'get_runtime_config', lambda: SimpleNamespace(embedding_enabled=True))
    monkeypatch.setattr(search.embedding_client, 'embed_query', AsyncMock(return_value=[1.0]))
    memory = MemoryRecord(id=uuid4(), content='Owns Atlas', category='fact', revision=1,
                          audience='both', pinned=False)
    retrieve = AsyncMock(return_value=SimpleNamespace(items=[memory]))
    monkeypatch.setattr(search.MemoryRetrievalService, 'retrieve', retrieve)
    episode = EpisodeReference(uuid4(), 'Atlas', uuid4(), datetime.now(UTC), 'user',
                               'Atlas address: http://192.0.2.73', ())
    episodes = AsyncMock(return_value=SimpleNamespace(references=[episode]))
    monkeypatch.setattr(search.EpisodeRetrievalService, 'retrieve', episodes)
    monkeypatch.setattr(search.GlobalSearchService, 'session_titles', AsyncMock(return_value=[]))

    result = json.loads((await tool_registry.execute(call, context)).content)

    assert result['sessions'][0]['passage'] == episode.anchor_content
    assert result['sources'] == {'memories': memory_status, 'sessions': 'searched'}
    assert retrieve.await_count == int(memory_status == 'searched')
    assert episodes.await_args.kwargs['readable_by'] == (TrustLevel.PRIVATE if include_private else TrustLevel.PUBLIC)
    assert episodes.await_args.kwargs['project_id'] == (None if all_projects else context.session.project_id)
    assert search.GlobalSearchService.session_titles.await_args.kwargs['project_id'] == (None if all_projects else context.session.project_id)
    assert [call.args[0].capability for call in context.ask_handler.await_args_list if call.args[0].resource_type == 'session_collection'] == ['session.read_sensitive' if include_private else 'session.search']


@pytest.mark.parametrize(('legacy', 'capability'), [('memory_retrieve', 'memory.read'), ('session_search', 'session.search')])
def test_legacy_source_switch_and_delegation_ceiling_do_not_widen_access(legacy, capability):
    source = 'memories' if legacy == 'memory_retrieve' else 'sessions'
    settings = ToolSettings(tool_availability={legacy: False})
    assert not search.source_available(settings, source)
    assert 'context_search' in tool_registry.effective(IntegrationSettings(), memory_enabled=True, tool_settings=settings).tools
    settings.tool_availability.clear()
    settings.private_permissions.policies[capability] = 'disabled'
    assert not search.source_available(settings, source)
    settings._delegation_ceiling = {'contextSources': []}
    assert not search.source_available(settings, source)


@pytest.mark.parametrize('scope', ['all', 'sessions'])
async def test_scoped_context_search_uses_session_search_permission_and_no_memory(tmp_path, monkeypatch, scope):
    session_id = uuid4()
    call = ToolCall('search', 'context_search', {'query': 'port', 'session_id': str(session_id), 'scope': scope})
    context = _context(tmp_path, call)
    context.model_trust_level = TrustLevel.PUBLIC
    context.db.scalar.return_value = ChatSession(id=session_id, user_id=context.session.user_id, trust_level='public')
    monkeypatch.setattr(search.access, 'profile_tool_settings', AsyncMock(return_value=context.tool_settings))
    monkeypatch.setattr(search, 'get_runtime_config', lambda: SimpleNamespace(embedding_enabled=False))
    episodes = AsyncMock(return_value=SimpleNamespace(references=[]))
    monkeypatch.setattr(search.EpisodeRetrievalService, 'retrieve', episodes)

    result = json.loads((await tool_registry.execute(call, context)).content)

    assert result['sources'] == {'sessions': 'searched'}
    assert context.ask_handler.call_args.args[0].capability == 'session.search'
    assert episodes.call_args.kwargs['session_id'] == session_id
    assert episodes.call_args.kwargs['readable_by'] == TrustLevel.PUBLIC


def test_context_search_history_revalidates_memory_audience_even_when_nested():
    content = json.dumps({'memories': [{'audience': 'private_only', 'content': 'secret'},
                                       {'audience': 'both', 'content': 'visible'}], 'sessions': []})
    nested = json.dumps({'messages': [{'role': 'tool', 'toolName': 'context_search', 'content': content}]})
    projected = _target_tool_result('session_read', nested, TrustLevel.PUBLIC)
    assert 'secret' not in projected and 'visible' in projected
