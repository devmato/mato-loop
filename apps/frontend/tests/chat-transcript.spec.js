import { describe, expect, it } from 'vitest'

import {
  buildChatTranscript,
  groupWorkHistoryEntries,
  retryAvailableForMessage,
  withLiveTranscriptMessage
} from '../src/utils/chat-transcript'

describe('chat transcript', () => {
  it('keeps a direct in-run message inside the same assistant turn', () => {
    const transcript = buildChatTranscript([
      { id: 'start', role: 'user', content: 'Start' },
      { id: 'partial', role: 'assistant', content: 'Working', runId: 'run-1', continuationSegment: true },
      { id: 'direct', role: 'user', content: 'Change course', continuationRunId: 'run-1' },
      { id: 'final', role: 'assistant', content: 'Done', runId: 'run-1' }
    ])

    expect(transcript.messages.filter((message) => message.role === 'assistant').map((message) => (
      [message.id, message.showAgentLabel, message.content]
    ))).toEqual([['final', true, 'Working\n\nDone']])
    expect(transcript.messages.find((message) => message.id === 'direct').content).toBe('Change course')
  })

  it('keeps streamed and persisted partial text in the continued answer', () => {
    const messages = [
      { id: 'start', role: 'user', content: 'Start' },
      { id: 'partial', role: 'assistant', content: 'Before', runId: 'run-1', continuationSegment: true },
      { id: 'direct', role: 'user', content: 'Change', continuationRunId: 'run-1' }
    ]
    const live = { id: 'live', role: 'assistant', content: 'After', currentTurn: true }

    expect(withLiveTranscriptMessage(buildChatTranscript(messages, [], { running: true, runId: 'run-1' }).messages, live)
      .at(-1).content).toBe('Before\n\nAfter')
    expect(buildChatTranscript([...messages, {
      id: 'final', role: 'assistant', content: 'After', runId: 'run-1'
    }]).messages.at(-1).content).toBe('Before\n\nAfter')
  })

  it('hides an empty redirected turn while retaining explicit stops and visible partial responses', () => {
    const user = { id: 'user', role: 'user', content: 'Start' }
    const empty = { id: 'empty', role: 'assistant', content: '', generationStatus: 'interrupted', redirected: true,
      reasoning: { kind: 'content', text: 'Unfertiger Gedanke', status: 'interrupted' }, activeDurationMs: 3000 }
    const explicit = { ...empty, id: 'stopped', redirected: false }
    const partial = { ...empty, id: 'partial', content: 'Angefangene Antwort' }

    expect(buildChatTranscript([user, empty, { id: 'correction', role: 'user', content: 'Anders' }])
      .messages.map((message) => message.id)).toEqual(['user', 'correction'])
    expect(buildChatTranscript([user, explicit]).messages.at(-1).id).toBe('stopped')
    expect(buildChatTranscript([user, partial]).messages.at(-1)).toMatchObject({
      id: 'partial', content: 'Angefangene Antwort', redirected: true
    })
  })

  it('keeps the last tool-round text visible after a manual stop', () => {
    const transcript = buildChatTranscript([
      { id: 'user', role: 'user', content: 'Start' },
      { id: 'step', role: 'assistant', content: 'Ich prüfe die Datei.',
        toolCalls: [{ id: 'read', name: 'read_file', arguments: {} }] },
      { id: 'stopped', role: 'assistant', content: '', generationStatus: 'interrupted' }
    ])

    expect(transcript.messages.filter((message) => message.role === 'assistant' && message.content)
      .map((message) => message.content)).toEqual(['Ich prüfe die Datei.'])
  })

  it('exposes validated file actions on the final assistant response', () => {
    const sessionId = '550e8400-e29b-41d4-a716-446655440000'
    const path = 'Support-Meeting-Protokoll.md'
    const attachment = {
      fileId: '550e8400-e29b-41d4-a716-446655440001', fileRevision: 1,
      fileRootId: sessionId, filePath: path, filename: path, sizeBytes: 42
    }
    const transcript = buildChatTranscript([
      { id: 'user', role: 'user', content: 'Erstelle die Datei' },
      {
        id: 'assistant-tool', role: 'assistant', content: '',
        toolCalls: [{ id: 'call', name: 'provide_file', arguments: { path } }]
      },
      { id: 'result', role: 'tool', toolCallId: 'call', content: '{}', attachments: [attachment] },
      { id: 'assistant-final', role: 'assistant', content: `Hier ist **${path}**.` }
    ])

    expect(transcript.messages.at(-1).fileActions).toEqual([{
      operation: 'provide', root: 'session', rootIdentity: sessionId,
      path, filename: path, sizeBytes: 42, sessionId, fileRevision: 1
    }])
  })

  it.each([
    [false, 'write', 'fileChanges'], [true, 'write', 'fileChanges'],
    [false, 'provide', 'providedFiles'], [true, 'provide', 'providedFiles']
  ])('keeps file outputs at the end of the answer (streaming: %s, operation: %s)', (streaming, operation, field) => {
    const rootIdentity = '550e8400-e29b-41d4-a716-446655440000'
    const action = {
      operation, root: 'session', rootIdentity, path: 'result.txt', filename: 'result.txt', sizeBytes: 7,
      downloadUrl: `/api/workspace/files/download?scope=session&root_identity=${rootIdentity}&session_id=${rootIdentity}&path=result.txt`
    }
    const transcript = buildChatTranscript([
      { id: 'user', role: 'user', content: 'Erstelle die Datei' },
      { id: 'tool', role: 'assistant', toolCalls: [{ id: 'call', name: 'write_file', arguments: {} }] },
      { id: 'result', role: 'tool', toolCallId: 'call', fileActions: [action] },
      ...(!streaming ? [{ id: 'final', role: 'assistant', content: 'Fertig' }] : [])
    ], [], { running: streaming })
    const messages = streaming ? withLiveTranscriptMessage(transcript.messages, {
      id: 'live', role: 'assistant', content: 'Fertig', currentTurn: true
    }) : transcript.messages

    expect(messages.filter((message) => message[field]?.length).map((message) => [
      message.id, message[field].map((change) => change.path)
    ])).toEqual([[streaming ? 'live' : 'final', ['result.txt']]])
  })

  it('offers retry only for the current backend-approved failed response', () => {
    const error = {
      id: 'error-1', role: 'assistant', kind: 'runtime_error', retryAvailable: true
    }

    expect(retryAvailableForMessage(error, [{ id: 'user-1' }, error])).toBe(true)
    expect(retryAvailableForMessage(error, [error, { id: 'user-2' }])).toBe(false)
    expect(retryAvailableForMessage(error, [error], true)).toBe(false)
    expect(retryAvailableForMessage({ ...error, retryAvailable: false }, [error])).toBe(false)
  })

  it('keeps the model response visible above the host error', () => {
    const transcript = buildChatTranscript([
      { id: 'user', role: 'user', content: 'Start' },
      { id: 'answer', role: 'assistant', content: 'Konvertierung noch offen.' },
      { id: 'error', role: 'assistant', kind: 'runtime_error', error: { code: 'agent.toolCallLimitReached' } }
    ])

    expect(transcript.messages.filter((message) => message.role === 'assistant')
      .map((message) => [message.id, message.content])).toEqual([
      ['answer', 'Konvertierung noch offen.'],
      ['error', 'Der Agent hat die maximale Anzahl an Tool-Aufrufen erreicht.']
    ])
  })

  it('keeps the virtual tail length stable when live content becomes canonical', () => {
    const user = { id: 'user-1', role: 'user', content: 'Frage' }
    const liveHistory = { id: 'live-history', entries: [] }
    const live = {
      id: 'live-session-1', role: 'assistant', content: 'Antwort', currentTurn: true,
      workHistory: liveHistory
    }

    const streaming = withLiveTranscriptMessage([user], live)
    const canonical = { id: 'assistant-1', role: 'assistant', content: 'Antwort', currentTurn: false }
    const completed = withLiveTranscriptMessage(
      [user, canonical],
      { ...live, content: '' }
    )

    expect(streaming).toHaveLength(2)
    expect(streaming[1]).toBe(live)
    expect(completed).toHaveLength(2)
    expect(completed[1]).toEqual({
      ...canonical,
      liveMessageId: live.id,
      workHistory: liveHistory
    })
    expect(canonical).not.toHaveProperty('workHistory')
  })

  it('does not group different tools or cross other activity', () => {
    const read = { key: 'read', type: 'tool', tool: { name: 'read_file' } }
    const search = { key: 'search', type: 'tool', tool: { name: 'grep' } }
    const note = { key: 'note', type: 'commentary', message: { content: 'Bearbeitung beginnt' } }
    const list = { key: 'list', type: 'tool', tool: { name: 'list_directory' } }

    expect(groupWorkHistoryEntries([read, search, note, list])).toEqual([
      read,
      search,
      note,
      list
    ])
    expect(groupWorkHistoryEntries([read, search], search)).toEqual([
      read,
      search
    ])
  })

  it('groups consecutive identical tools without crossing other activity', () => {
    const first = { key: 'first', type: 'tool', tool: { name: 'web_search' }, result: {} }
    const second = { key: 'second', type: 'tool', tool: { name: 'web_search' }, result: {} }
    const note = { key: 'note', type: 'commentary', message: { content: 'Zwischenstand' } }
    const third = { key: 'third', type: 'tool', tool: { name: 'web_search' }, result: {} }
    const current = { key: 'current', type: 'tool', tool: { name: 'web_search' }, active: true }

    expect(groupWorkHistoryEntries([first, second, note, third, current], current)).toEqual([
      {
        key: 'tool-group-first', type: 'tool-group', tool: first.tool,
        items: [first, second]
      },
      note,
      {
        key: 'tool-group-third', type: 'tool-group', tool: third.tool,
        items: [third, current], active: true
      }
    ])
  })

  it('marks a completed tool immediately while its canonical result is refreshing', () => {
    const transcript = buildChatTranscript([
      { id: 'user', role: 'user', content: 'Start' },
      {
        id: 'assistant', role: 'assistant', content: '',
        toolCalls: [{ id: 'call', name: 'web_search', arguments: { query: 'MatoLoop' } }]
      }
    ], [], { running: true, runId: 'run', completedToolCallId: 'call' })
    const entry = transcript.messages[1].workHistory.entries[0]

    expect(entry).toMatchObject({ completed: true, result: null })
  })

  it('keeps privacy warnings in persisted assistant work history', () => {
    const transcript = buildChatTranscript([
      { id: 'user-1', role: 'user', content: 'Frage', createdAt: '2026-08-06T12:00:00Z' },
      {
        id: 'assistant-1', role: 'assistant', content: 'Antwort', toolCalls: [],
        warnings: ['privacy.placeholderRestorationIncomplete'],
        createdAt: '2026-08-06T12:00:01Z'
      }
    ])

    expect(transcript.messages[1].workHistory.entries).toEqual([expect.objectContaining({
      type: 'warning',
      warning: true,
      presentation: expect.objectContaining({ title: 'Geschützter Wert nicht verfügbar' })
    })])
  })

  it('attaches results and approvals to their tool calls', () => {
    const messages = [
      { id: 'user-1', role: 'user', content: 'Lies die Datei' },
      {
        id: 'assistant-1',
        role: 'assistant',
        content: '',
        toolCalls: [{ id: 'call-1', name: 'read_file', arguments: { path: 'plan.md' } }]
      },
      { id: 'result-1', role: 'tool', toolCallId: 'call-1', content: 'Inhalt' }
    ]
    const approvals = [{ id: 'approval-1', details: { toolCallId: 'call-1' } }]

    const transcript = buildChatTranscript(messages, approvals, { running: true })

    expect(transcript.messages).toHaveLength(2)
    expect(transcript.messages[1].workHistory.entries[0]).toMatchObject({
      result: messages[2],
      approval: approvals[0],
      active: false
    })
    expect(transcript.unmatchedApprovals).toEqual([])
  })

  it('keeps approvals without a matching visible call as fallback cards', () => {
    const approval = { id: 'approval-1', details: { toolCallId: 'missing' } }
    expect(buildChatTranscript([], [approval]).unmatchedApprovals).toEqual([approval])
  })

  it('attaches pending questions to their tool call', () => {
    const messages = [
      { id: 'user-1', role: 'user', content: 'Plane den Ausflug' },
      {
        id: 'assistant-1', role: 'assistant', content: '',
        toolCalls: [{ id: 'call-1', name: 'question', arguments: {} }]
      }
    ]
    const question = { id: 'question-1', toolCallId: 'call-1', questions: [] }

    const transcript = buildChatTranscript(messages, [], { running: true, questions: [question] })

    expect(transcript.messages[1].workHistory.entries[0]).toMatchObject({
      question,
      active: false
    })
    expect(transcript.unmatchedQuestions).toEqual([])
  })

  it('marks only the next unresolved sequential call as running', () => {
    const messages = [
      {
        id: 'user-1', role: 'user', content: 'Arbeite die Schritte ab',
        createdAt: '2026-08-06T12:00:00Z'
      },
      {
        id: 'assistant-1',
        role: 'assistant',
        content: '',
        createdAt: '2026-08-06T12:00:01Z',
        toolCalls: [
          { id: 'call-1', name: 'read_file', arguments: { path: 'eins.md' } },
          { id: 'call-2', name: 'read_file', arguments: { path: 'zwei.md' } }
        ]
      }
    ]

    const history = buildChatTranscript(messages, [], { running: true })
      .messages[1].workHistory
    const calls = history.entries

    expect(calls.map((call) => call.active)).toEqual([true, false])
    expect(history).toMatchObject({
      startedAt: '2026-08-06T12:00:00Z',
      completedAt: null
    })
  })

  it('uses the runtime tool call id instead of stale result ordering', () => {
    const messages = [
      { id: 'user-1', role: 'user', content: 'Arbeite die Schritte ab' },
      {
        id: 'assistant-1',
        role: 'assistant',
        content: '',
        toolCalls: [
          { id: 'call-1', name: 'read_file', arguments: { path: 'eins.md' } },
          { id: 'call-2', name: 'read_file', arguments: { path: 'zwei.md' } }
        ]
      }
    ]

    const calls = buildChatTranscript(messages, [], {
      running: true,
      activeToolCallId: 'call-2'
    }).messages[1].workHistory.entries

    expect(calls.map((call) => call.active)).toEqual([false, true])
  })

  it('does not keep a stale unresolved call active during model processing', () => {
    const messages = [
      { id: 'user-1', role: 'user', content: 'Lies die Datei' },
      {
        id: 'assistant-1',
        role: 'assistant',
        content: '',
        toolCalls: [{ id: 'call-1', name: 'read_file', arguments: { path: 'eins.md' } }]
      }
    ]

    const calls = buildChatTranscript(messages, [], {
      running: true,
      toolExecutionActive: false
    }).messages[1].workHistory.entries

    expect(calls[0].active).toBe(false)
  })

  it('does not mark calls after an open approval as running', () => {
    const messages = [
      { id: 'user-1', role: 'user', content: 'Arbeite die Schritte ab' },
      {
        id: 'assistant-1',
        role: 'assistant',
        content: '',
        toolCalls: [
          { id: 'call-1', name: 'write_file', arguments: { path: 'eins.md' } },
          { id: 'call-2', name: 'read_file', arguments: { path: 'zwei.md' } }
        ]
      }
    ]
    const approvals = [{ id: 'approval-1', details: { toolCallId: 'call-1' } }]

    const calls = buildChatTranscript(messages, approvals, { running: true })
      .messages[1].workHistory.entries

    expect(calls.map((call) => call.active)).toEqual([false, false])
  })

  it('groups reasoning and tools chronologically into one work history', () => {
    const messages = [
      {
        id: 'user-1', role: 'user', content: 'Recherchiere das',
        createdAt: '2026-08-06T12:00:00Z'
      },
      {
        id: 'assistant-1',
        role: 'assistant',
        content: 'Ich starte mit einer Suche.',
        createdAt: '2026-08-06T12:00:01Z',
        trustLevel: 'private',
        reasoning: { kind: 'summary', text: 'Suche planen', observedDurationMs: 200, reasoningTokens: 10 },
        toolCalls: [{ id: 'call-1', name: 'web_search', arguments: { query: 'MatoLoop' } }]
      },
      { id: 'result-1', role: 'tool', toolCallId: 'call-1', content: 'Treffer' },
      {
        id: 'assistant-2',
        role: 'assistant',
        content: '',
        reasoning: { kind: 'summary', text: 'Treffer prüfen', observedDurationMs: 300, reasoningTokens: 20 },
        toolCalls: [{ id: 'call-2', name: 'webfetch', arguments: { url: 'https://example.com' } }]
      },
      { id: 'result-2', role: 'tool', toolCallId: 'call-2', content: 'Seite' },
      {
        id: 'assistant-3', role: 'assistant', content: 'Ergebnis', toolCalls: [],
        modelName: 'Antwortmodell', trustLevel: 'public', createdAt: '2026-08-06T12:00:03Z',
        activeDurationMs: 2_400
      }
    ]

    const transcript = buildChatTranscript(messages)

    expect(transcript.messages.map((message) => message.id))
      .toEqual(['user-1', 'assistant-1', 'assistant-3'])
    expect(transcript.messages[1].workHistory).toMatchObject({
      observedDurationMs: 500,
      activeDurationMs: 2_400,
      activeDurationUpdatedAt: null,
      startedAt: '2026-08-06T12:00:00Z',
      completedAt: '2026-08-06T12:00:03Z'
    })
    expect(transcript.messages[1].workHistory.entries.map((entry) => (
      entry.reasoning?.text || entry.message?.content || entry.tool.name
    ))).toEqual([
      'Suche planen',
      'Ich starte mit einer Suche.',
      'web_search',
      'Treffer prüfen',
      'webfetch'
    ])
    expect(transcript.messages[1]).toMatchObject({
      showAgentLabel: true,
      continuesAssistantTurn: true,
      content: '',
      createdAt: null,
      modelName: null,
      trustLevel: null
    })
    expect(transcript.messages[2]).toMatchObject({
      hasAssistantContent: true,
      createdAt: '2026-08-06T12:00:03Z',
      modelName: 'Antwortmodell',
      trustLevel: 'private'
    })
  })

  it('moves all intermediate commentary into chronological history and keeps the final once', () => {
    const source = { title: 'Quelle', url: 'https://example.test' }
    const attachment = { id: 'attachment-1', filename: 'plan.md' }
    const transcript = buildChatTranscript([
      { id: 'user', role: 'user', content: 'Arbeite' },
      {
        id: 'commentary-1', role: 'assistant', content: '**Plan**',
        research: { linkedSources: [source] }, attachments: [attachment], toolCalls: []
      },
      {
        id: 'tool-round', role: 'assistant', content: '',
        toolCalls: [{ id: 'call', name: 'web_search', arguments: {} }]
      },
      { id: 'tool-result', role: 'tool', toolCallId: 'call', content: 'Treffer' },
      { id: 'commentary-2', role: 'assistant', content: 'Prüfe den Treffer.', toolCalls: [] },
      { id: 'final', role: 'assistant', content: 'Fertige Antwort', toolCalls: [] }
    ])

    expect(transcript.messages.map((message) => message.id)).toEqual(['user', 'commentary-1', 'final'])
    expect(transcript.messages[1].workHistory.entries.map((entry) => (
      entry.message?.content || entry.tool.name
    ))).toEqual(['**Plan**', 'web_search', 'Prüfe den Treffer.'])
    expect(transcript.messages[1].workHistory.entries[0].message).toMatchObject({
      research: { linkedSources: [source] }, attachments: [attachment]
    })
    expect(transcript.messages.filter((message) => message.content === 'Fertige Antwort')).toHaveLength(1)
    expect(transcript.messages[1].attachments).toEqual([])
  })

  it('creates duration metadata for a plain completed answer', () => {
    const transcript = buildChatTranscript([
      { id: 'user-1', role: 'user', content: 'Hallo' },
      { id: 'assistant-1', role: 'assistant', content: 'Hi', activeDurationMs: 750 }
    ])

    expect(transcript.messages[1].workHistory).toMatchObject({
      entries: [],
      activeDurationMs: 750,
      activeDurationUpdatedAt: null
    })
    expect(transcript.messages[1].activeDurationMs).toBe(750)
  })

  it('places the completed duration on history when the turn has activity', () => {
    const messages = [
      { id: 'user', role: 'user', content: 'Prüfen' },
      { id: 'step', role: 'assistant', content: 'Ich prüfe das.', activeDurationMs: 1_000,
        toolCalls: [{ id: 'call', name: 'web_search', arguments: {} }] },
      { id: 'result', role: 'tool', toolCallId: 'call', content: '{}' },
      { id: 'final', role: 'assistant', content: 'Ergebnis', activeDurationMs: 57_000 }
    ]
    const completed = buildChatTranscript(messages).messages
    expect(completed.find((message) => message.id === 'step').activeDurationMs).toBeNull()
    expect(completed.find((message) => message.id === 'step').workHistory.activeDurationMs).toBe(57_000)
    expect(completed.at(-1).activeDurationMs).toBeNull()
    const live = buildChatTranscript(messages, [], { running: true }).messages
    expect(live.filter((message) => message.role === 'assistant').every((message) => message.activeDurationMs === null)).toBe(true)
    expect(messages[1].activeDurationMs).toBe(1_000)
  })

  it('keeps the newest prepared reply with its assistant turn', () => {
    const preparedReply = {
      markdown: '[Kunde 123](#reply-customer-123)',
      options: [{ id: 'customer-123', content: 'Verwende ERP-Kunde 123.' }]
    }
    const transcript = buildChatTranscript([
      { id: 'user', role: 'user', content: 'Muster' },
      {
        id: 'tool-call', role: 'assistant', content: '',
        toolCalls: [{ id: 'call', name: 'tbs_desk_select_customer', arguments: {} }]
      },
      { id: 'result', role: 'tool', toolCallId: 'call', content: '{}', preparedReply },
      { id: 'final', role: 'assistant', content: 'Bitte auswählen.', toolCalls: [] }
    ])

    expect(transcript.messages[1].preparedReply).toEqual(preparedReply)
  })

  it('starts a new assistant turn for delivered automation messages', () => {
    const transcript = buildChatTranscript([
      { id: 'user-1', role: 'user', content: 'Erinnere mich später' },
      { id: 'assistant-1', role: 'assistant', content: 'Automation erstellt' },
      {
        id: 'automation-1', role: 'assistant', content: 'Erinnerung',
        sourceType: 'automationRun', createdAt: '2026-08-20T14:20:00Z'
      }
    ])

    expect(transcript.messages).toHaveLength(3)
    expect(transcript.messages[1]).toMatchObject({
      showAgentLabel: true,
      continuesAssistantTurn: false
    })
    expect(transcript.messages[2]).toMatchObject({
      showAgentLabel: true,
      continuesAssistantTurn: false,
      createdAt: '2026-08-20T14:20:00Z'
    })
  })

  it('places persisted context summaries chronologically and marks host fallbacks', () => {
    const transcript = buildChatTranscript([
      {
        id: '11111111-1111-4111-8111-111111111111', role: 'user', content: 'Frage',
        createdAt: '2026-08-27T10:00:00Z'
      },
      {
        id: '22222222-2222-4222-8222-222222222222', role: 'assistant', content: 'Zwischenstand',
        createdAt: '2026-08-27T10:00:01Z'
      },
      {
        id: '44444444-4444-4444-8444-444444444444', role: 'assistant', content: 'Fortsetzung',
        createdAt: '2026-08-27T10:00:03Z'
      }
    ], [], {
      contextSummaries: [{
        id: '33333333-3333-4333-8333-333333333333',
        coveredThroughMessageId: '22222222-2222-4222-8222-222222222222',
        content: 'Goal\nWeiterarbeiten',
        isFallback: true,
        createdAt: '2026-08-27T10:00:02Z'
      }]
    })

    expect(transcript.messages.map((message) => message.id)).toEqual([
      '11111111-1111-4111-8111-111111111111',
      '22222222-2222-4222-8222-222222222222',
      '44444444-4444-4444-8444-444444444444'
    ])
    const summaryEntry = transcript.messages[1].workHistory.entries.find((entry) => entry.type === 'context-summary')
    expect(summaryEntry).toMatchObject({
      warning: true,
      text: 'Goal\nWeiterarbeiten',
      presentation: {
        title: 'Gesprächskontext verdichtet',
        summary: 'Vereinfachte Ersatzfassung'
      }
    })
    expect(transcript.messages[1].workHistory.entries.map((entry) => entry.type))
      .toEqual(['commentary', 'context-summary'])
  })

  it('uses an attributed assistant as carrier when a context summary comes first', () => {
    const transcript = buildChatTranscript([
      { id: 'user', role: 'user', content: 'Weiter', createdAt: '2026-09-01T10:00:00Z' },
      {
        id: 'answer', role: 'assistant', content: 'Fertig', agentProfileId: 'profile-1',
        createdAt: '2026-09-01T10:00:02Z'
      }
    ], [], {
      contextSummaries: [{
        id: 'summary', content: 'Goal\nWeiter', isFallback: false,
        createdAt: '2026-09-01T10:00:01Z'
      }]
    })

    expect(transcript.messages.map((message) => message.id)).toEqual(['user', 'answer'])
    expect(transcript.messages[1]).toMatchObject({
      agentProfileId: 'profile-1',
      workHistory: { entries: [expect.objectContaining({ type: 'context-summary' })] }
    })
  })

  it('attributes a live-only context summary from the active run message', () => {
    const transcript = buildChatTranscript([
      { id: 'user', role: 'user', content: 'Weiter', createdAt: '2026-09-01T10:00:00Z' }
    ], [], {
      running: true,
      contextSummaries: [{
        id: 'summary', content: 'Goal\nWeiter', isFallback: false,
        createdAt: '2026-09-01T10:00:01Z'
      }]
    })
    const messages = withLiveTranscriptMessage(transcript.messages, {
      id: 'live-session', role: 'assistant', content: '', agentProfileId: 'profile-live'
    })

    expect(messages[1]).toMatchObject({
      agentProfileId: 'profile-live',
      workHistory: { entries: [expect.objectContaining({ type: 'context-summary' })] }
    })

    const streaming = withLiveTranscriptMessage(transcript.messages, {
      id: 'live-session', role: 'assistant', content: 'Antwort', agentProfileId: 'profile-live'
    })
    expect(streaming[1].agentProfileId).toBe('profile-live')
    expect(streaming[2].content).toBe('Antwort')
  })

  it('does not mix reused tool call ids across user requests', () => {
    const messages = [
      { id: 'user-1', role: 'user', content: 'Erste Anfrage' },
      {
        id: 'assistant-1', role: 'assistant', content: '',
        toolCalls: [{ id: 'shared', name: 'read_file', arguments: {} }]
      },
      { id: 'result-1', role: 'tool', toolCallId: 'shared', content: 'Erstes Ergebnis' },
      { id: 'user-2', role: 'user', content: 'Zweite Anfrage' },
      {
        id: 'assistant-2', role: 'assistant', content: '',
        toolCalls: [{ id: 'shared', name: 'read_file', arguments: {} }]
      },
      { id: 'result-2', role: 'tool', toolCallId: 'shared', content: 'Zweites Ergebnis' }
    ]

    const histories = buildChatTranscript(messages).messages
      .filter((message) => message.workHistory)
      .map((message) => message.workHistory)

    expect(histories[0].entries[0].result.content).toBe('Erstes Ergebnis')
    expect(histories[1].entries[0].result.content).toBe('Zweites Ergebnis')
  })
})
