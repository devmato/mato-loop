import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() } }))
vi.mock('src/utils/notifications', () => ({ notifyError: vi.fn() }))

import { api } from 'src/boot/axios'
import { notifyError } from 'src/utils/notifications'
import { useSubagentStore, subagentStatus } from '../src/stores/subagents'
import { useChatStore } from '../src/features/chat/store'
import { useApprovalStore } from '../src/stores/approvals'
import { useQuestionStore } from '../src/stores/questions'
import { buildChatTranscript } from '../src/utils/chat-transcript'
import { agentRunPresentation } from '../src/utils/agent-activity'

class EventSourceMock {
  static instances = []
  constructor(url) {
    this.url = url
    this.listeners = new Map()
    this.close = vi.fn()
    EventSourceMock.instances.push(this)
  }
  addEventListener(name, handler) { this.listeners.set(name, handler) }
  dispatchEvent(event) { this.listeners.get(event.type)?.(event) }
  emit(name, payload, lastEventId = '') { this.listeners.get(name)?.({ data: JSON.stringify(payload), lastEventId }) }
}

const deferred = () => {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
const childRun = { id: 'child-run', status: 'running' }
const child = { id: 'child', title: 'Dateien untersuchen', parentSessionId: 'parent', spawnedByRunId: 'parent-run', spawnedByToolCallId: 'task-call', spawnIndex: 0, run: childRun }
let store, session, run, chat

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  EventSourceMock.instances = []
  vi.stubGlobal('EventSource', EventSourceMock)
  vi.stubGlobal('window', { localStorage: { getItem: () => null, setItem() {}, removeItem() {} } })
  store = useSubagentStore()
  chat = useChatStore()
  run = { ...childRun }
  session = { id: 'child', title: child.title, parentSessionId: 'parent', readOnly: true, activeRun: run, messages: [], nextMessageCursor: 'older' }
  api.get.mockImplementation(async (url) => {
    if (url.endsWith('/children')) return { data: [child] }
    if (url.includes('/runs/')) return { data: { ...run } }
    if (url === '/sessions/child') return { data: structuredClone(session) }
    if (url === '/sessions/child/messages') return { data: { items: [{ id: 'old', role: 'user', content: 'Auftrag' }], nextCursor: null } }
    if (url.endsWith('/pending')) return { data: [] }
    return { data: { items: [] } }
  })
})

afterEach(() => {
  store.close()
  chat.eventSource?.close()
  chat.cancelQueueDrain()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('read-only subagents', () => {
  it.each(['parent', 'child'])('replaces snapshot buffers and clears finished compaction for %s', async (consumer) => {
    chat.current = { id: 'parent', messages: [] }
    chat.watchRun('parent', 'parent-run', { status: 'running' })
    await store.loadChildren('parent')
    await store.open('parent', 'child')
    const target = consumer === 'parent' ? chat : store
    const source = EventSourceMock.instances[consumer === 'parent' ? 0 : 1]
    api.get.mockImplementation(() => new Promise(() => {}))
    const initialPhase = target.activity.phase
    source.emit('run.snapshot', { content: '', events: [] })
    expect(target.activity.phase).toBe(initialPhase)
    const content = Array.from({ length: 350 }, (_, index) => `[${index}]`).join('')
    const snapshot = {
      content,
      events: [
        { type: 'run.phase', phase: 'waiting_model' },
        { type: 'run.timing', activeTimingRevision: 3, activeDurationMs: 9000, activeDurationRunning: false },
        { type: 'process.output', toolCallId: 'shell', output: { stdout: 'Output', stderr: '' } }
      ],
      researchSources: [{ url: 'https://example.com/', title: 'Source' }]
    }
    source.emit('message.delta', { delta: 'Old suffix' })
    source.emit('run.snapshot', snapshot)
    source.emit('message.delta', { delta: ' next' })
    expect(target.streamContent).toBe(`${content} next`)
    expect(target.processOutputs.shell.stdout).toBe('Output')
    source.emit('process.output', { toolCallId: 'shell', stream: 'stdout', delta: ' next' })
    expect(target.processOutputs.shell.stdout).toBe('Output next')
    expect(target.streamResearchSources).toEqual(snapshot.researchSources)
    expect(target.activity).toMatchObject({ phase: 'streaming', activeDurationMs: 9000 })
    source.emit('run.snapshot', { ...snapshot, content: `${content} next` })
    expect(target.streamContent).toBe(`${content} next`)
    source.emit('process.output', { toolCallId: 'shell', stream: 'stdout', delta: ' again' })
    expect(target.processOutputs.shell.stdout).toBe('Output again')
    source.emit('message.continued', {})
    source.emit('message.delta', { delta: 'New answer' })
    expect(target.streamContent).toBe('New answer')

    source.emit('run.phase', { phase: 'compacting_context' })
    source.emit('context.summary.updated', { summary: { content: 'Summary' } })
    expect(target.streamContextSummary.content).toBe('Summary')
    source.emit('context.compacted', { summary: { id: 'summary', content: 'Summary' } })
    expect(target.streamContextSummary).toBeNull()
    expect(target.activity.phase).toBe('preparing_context')
    source.emit('run.phase', { phase: 'waiting_model', modelRequest: 2 })
    expect(target.activity.phase).toBe('waiting_model')
    source.emit('run.snapshot', {
      content: '', events: [
        { type: 'run.phase', phase: 'compacting_context' },
        { type: 'context.compacted', summary: { id: 'summary', content: 'Summary' } },
        { type: 'run.phase', phase: 'waiting_model', modelRequest: 3 }
      ]
    })
    expect(target.streamContextSummary).toBeNull()
    expect(target.activity.phase).toBe('waiting_model')
  })

  it.each(['parent', 'child'])('projects the full activity event sequence for %s', async (consumer) => {
    chat.current = { id: 'parent', messages: [] }
    chat.watchRun('parent', 'parent-run', { status: 'running' })
    await store.loadChildren('parent')
    await store.open('parent', 'child')
    const target = consumer === 'parent' ? chat : store
    const source = EventSourceMock.instances[consumer === 'parent' ? 0 : 1]
    // Keep canonical refresh pending to exercise the event-to-REST gap.
    api.get.mockImplementation(() => new Promise(() => {}))
    const presentation = () => agentRunPresentation({
      running: target.running, activity: target.activity,
      streamContent: target.streamContent, streamReasoning: target.streamReasoning,
      transcript: buildChatTranscript(target.current.messages, [], {
        running: target.running, toolExecutionActive: target.activity.phase === 'processing_tool',
        activeToolCallId: target.activity.toolCallId, completedToolCallId: target.completedToolCallId
      })
    })
    source.emit('run.started', {})
    expect(presentation().label).toBe('Verarbeite Anfrage')
    source.emit('run.phase', { phase: 'waiting_model', modelRequest: 1 })
    source.emit('message.delta', { delta: 'Ich prüfe das.' })
    expect(presentation().entry).toBeNull()
    const tool = { id: 'read', name: 'read_file', arguments: { path: 'README.md' } }
    source.emit('message.completed', { message: {
      id: 'step', role: 'assistant', content: 'Ich prüfe das.', toolCalls: [tool]
    } })
    expect(presentation().entry).toMatchObject({ type: 'tool', tool, active: true })
    expect(presentation().label).toBe('Datei lesen')
    source.emit('run.phase', { phase: 'processing_tool', tool: tool.name, toolCallId: tool.id })
    expect(presentation().entry).toMatchObject({ type: 'tool', tool, active: true })
    source.emit('run.timing', { activeDurationMs: 1_000, activeDurationRunning: true, activeTimingRevision: 2 })
    source.emit('run.phase', { phase: 'waiting_approval', activeTimingRevision: 2 })
    expect(presentation().entry.active).toBe(false)
    expect(target.activity.toolCallId).toBeNull()
    source.emit('run.phase', { phase: 'waiting_input' })
    expect(presentation().entry.active).toBe(false)
    source.emit('run.phase', { phase: 'processing_tool', tool: tool.name, toolCallId: tool.id })
    source.emit('tool.completed', { toolCallId: tool.id })
    expect(presentation().entry).toMatchObject({ type: 'tool', tool, active: true })
    source.emit('run.phase', { phase: 'waiting_model', modelRequest: 2 })
    expect(presentation().entry).toMatchObject({ type: 'tool', tool, active: true })
    expect(presentation().label).toBe('Datei lesen')
    expect(target.activity).toMatchObject({ tool: 'read_file', toolCallId: 'read' })
    source.emit('reasoning.updated', { reasoning: { kind: 'content', text: 'Ergebnis prüfen' } })
    expect(presentation().entry).toMatchObject({ type: 'reasoning', active: true, presentation: { title: 'Nachdenken' } })
    source.emit('message.completed', { message: {
      id: 'second', role: 'assistant', content: '',
      toolCalls: [{ id: 'search', name: 'web_search', arguments: { query: 'Neue Suche' } }]
    } })
    source.emit('run.phase', {
      phase: 'processing_tool', tool: 'web_search', toolCallId: 'search',
      toolDisplay: { group: 'web', icon: 'language', label: 'Websuche', summary: 'Neue Suche' }
    })
    source.emit('tool.completed', { toolCallId: 'search' })
    source.emit('run.phase', { phase: 'waiting_model', modelRequest: 3 })
    expect(presentation().entry).toMatchObject({ type: 'tool', tool: { id: 'search' }, active: true })
    expect(presentation().label).toBe('Websuche')
    source.emit('message.delta', { delta: 'Ergebnis' })
    expect(presentation().entry).toBeNull()
    source.emit('message.completed', { message: { id: 'answer', role: 'assistant', content: 'Ergebnis' } })
    expect(target.activity.phase).toBe('finalizing')
    expect(presentation().entry).toBeNull()
    source.emit('run.completed', {})
    expect(target.running).toBe(false)
    expect(presentation().entry).toBeNull()
  })

  it('discovers children with GET before any SSE and keeps only one child connection', async () => {
    await store.loadChildren('parent')
    expect(store.children).toEqual([child])
    expect(EventSourceMock.instances).toHaveLength(0)
    await store.open('parent', 'child')
    expect(EventSourceMock.instances[0].url).toBe('/api/sessions/child/runs/child-run/events')
    await store.open('parent', 'child')
    expect(EventSourceMock.instances[0].close).toHaveBeenCalled()
    expect(EventSourceMock.instances[1].close).not.toHaveBeenCalled()
    expect(api.post).not.toHaveBeenCalled()
    expect(api.patch).not.toHaveBeenCalled()
    expect(api.delete).not.toHaveBeenCalled()
  })

  it('discards invalidated and old-parent list responses', async () => {
    const first = deferred()
    api.get.mockReturnValueOnce(first.promise)
    const loading = store.loadChildren('parent')
    void store.loadChildren('parent')
    first.resolve({ data: [] })
    await loading
    expect(store.children).toEqual([child])
    const stale = deferred()
    api.get.mockReturnValueOnce(stale.promise)
    const pending = store.loadChildren('parent')
    await store.loadChildren('other-parent')
    stale.resolve({ data: [] })
    await pending
    expect(store.children).toEqual([child])
    expect(store.parentId).toBe('other-parent')
  })

  it('ignores late child responses after close and validates the parent/read-only boundary', async () => {
    const stale = deferred()
    api.get.mockReturnValueOnce(stale.promise)
    const opening = store.open('parent', 'child')
    store.close()
    stale.resolve({ data: session })
    await opening
    expect(store.current).toBeNull()
    session.parentSessionId = 'someone-else'
    await store.open('parent', 'child')
    expect(store.error).toEqual({ code: 'session.notFound' })
    expect(store.current).toBeNull()
    expect(EventSourceMock.instances).toHaveLength(0)
    session.parentSessionId = 'parent'
    session.readOnly = false
    await store.open('parent', 'child')
    expect(store.current).toBeNull()
  })

  it('streams deltas, reasoning and process output and deduplicates replayed messages and event IDs', async () => {
    await store.open('parent', 'child')
    const source = EventSourceMock.instances[0]
    source.emit('message.delta', { delta: 'Hallo' }, '1')
    source.emit('message.delta', { delta: 'Hallo' }, '1')
    source.emit('message.delta', { delta: ' Welt' }, '2')
    expect(store.streamContent).toBe('Hallo Welt')
    source.emit('reasoning.updated', { reasoning: { text: 'Prüfen' } })
    source.emit('process.output', { toolCallId: 'tool', stream: 'stdout', content: 'Ausgabe' })
    expect(store.streamReasoning.text).toBe('Prüfen')
    expect(store.processOutputs.tool.stdout).toBe('Ausgabe')
    const message = { id: 'answer', role: 'assistant', content: 'Hallo Welt', toolCalls: [{ id: 'tool', name: 'read' }] }
    session.messages = [message]
    source.emit('message.completed', { message })
    source.emit('message.completed', { message })
    await vi.waitFor(() => expect(store.current.messages).toEqual([message]))
    expect(store.streamContent).toBe('')
    source.emit('run.phase', { phase: 'processing_tool', toolCallId: 'tool', tool: 'read' })
    expect(store.activity.toolCallId).toBe('tool')
  })

  it('normalizes live run timing for the turn duration timer', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-12T12:00:05.000Z'))
    run = {
      ...run,
      activeDurationMs: 5_000,
      activeDurationRunning: true,
      activeTimingRevision: 1
    }

    await store.open('parent', 'child')
    expect(store.activity.activeDurationUpdatedAt).toBe('2026-09-12T12:00:05.000Z')

    const source = EventSourceMock.instances[0]
    source.emit('run.timing', {
      activeDurationMs: 9_000,
      activeDurationRunning: false,
      activeTimingRevision: 2
    })
    expect(store.activity).toMatchObject({ activeDurationMs: 9_000, activeDurationUpdatedAt: null })

    vi.setSystemTime(new Date('2026-09-12T12:00:10.000Z'))
    source.emit('run.timing', {
      activeDurationMs: 9_000,
      activeDurationRunning: true,
      activeTimingRevision: 3
    })
    expect(store.activity.activeDurationUpdatedAt).toBe('2026-09-12T12:00:10.000Z')
    source.emit('run.timing', {
      activeDurationMs: 1_000,
      activeDurationRunning: false,
      activeTimingRevision: 2
    })
    expect(store.activity).toMatchObject({
      activeDurationMs: 9_000,
      activeDurationRunning: true,
      activeTimingRevision: 3
    })
  })

  it('refetches canonical messages and run on connection loss and rebuilds the live buffer', async () => {
    vi.useFakeTimers()
    await store.loadChildren('parent')
    await store.open('parent', 'child')
    const source = EventSourceMock.instances[0]
    source.emit('message.delta', { delta: 'Teil' })
    source.onerror()
    expect(source.close).toHaveBeenCalled()
    expect(store.streamContent).toBe('')
    await vi.advanceTimersByTimeAsync(1000)
    expect(api.get).toHaveBeenCalledWith('/sessions/child/runs/child-run', expect.any(Object))
    expect(EventSourceMock.instances).toHaveLength(2)
    source.emit('message.delta', { delta: 'Veraltet' })
    const reconnected = EventSourceMock.instances[1]
    reconnected.emit('run.snapshot', { content: 'Vollständiger Teil', events: [] })
    reconnected.emit('message.delta', { delta: ' danach' })
    expect(store.streamContent).toBe('Vollständiger Teil danach')
    run.status = 'completed'
    session.activeRun = null
    session.messages = [{ id: 'final', role: 'assistant', content: 'Fertig' }]
    reconnected.onerror()
    await vi.advanceTimersByTimeAsync(5000)
    expect(store.current.messages[0].content).toBe('Fertig')
    expect(store.running).toBe(false)
    expect(EventSourceMock.instances).toHaveLength(2)
  })

  it('does not reopen a stream after navigating away during recovery', async () => {
    vi.useFakeTimers()
    await store.open('parent', 'child')
    const pending = deferred()
    api.get.mockReturnValueOnce(pending.promise)
    EventSourceMock.instances[0].onerror()
    store.close()
    pending.resolve({ data: session })
    await vi.advanceTimersByTimeAsync(10000)
    expect(EventSourceMock.instances).toHaveLength(1)
    expect(store.current).toBeNull()
  })

  it('restarts pagination after a disconnected gap and rejects an old in-flight page', async () => {
    vi.useFakeTimers()
    session.messages = [{ id: 'old-head', role: 'assistant', content: 'Alt' }]
    await store.open('parent', 'child')
    const page = deferred()
    api.get.mockReturnValueOnce(page.promise)
    const older = store.loadOlder()
    session.messages = [{ id: 'new-head', role: 'assistant', content: 'Neu' }]
    session.nextMessageCursor = 'gap-cursor'
    EventSourceMock.instances[0].onerror()
    await vi.advanceTimersByTimeAsync(1000)
    page.resolve({ data: { items: [{ id: 'too-old', role: 'user' }], nextCursor: null } })
    await older
    expect(store.current.messages.map((message) => message.id)).toEqual(['new-head'])
    expect(store.current.nextMessageCursor).toBe('gap-cursor')
  })

  it('stops repeated reconnection failure with the central safe stream error', async () => {
    vi.useFakeTimers()
    await store.open('parent', 'child')
    for (let index = 0; index < 4; index += 1) {
      EventSourceMock.instances.at(-1).onerror()
      await vi.advanceTimersByTimeAsync(4000)
    }
    expect(store.error).toEqual({ code: 'stream.connectionLost' })
    expect(notifyError).toHaveBeenCalledWith({ code: 'stream.connectionLost' })
  })

  it('loads older messages once, keeps newer results and ignores stale pages', async () => {
    await store.open('parent', 'child')
    await store.loadOlder()
    expect(store.current.messages.map((message) => message.id)).toEqual(['old'])
    expect(store.current.nextMessageCursor).toBeNull()
    const pending = deferred()
    store.current.nextMessageCursor = 'more'
    api.get.mockReturnValueOnce(pending.promise)
    const page = store.loadOlder()
    store.close()
    pending.resolve({ data: { items: [{ id: 'stale' }] } })
    await page
    expect(store.current).toBeNull()
  })

  it('does not let a stale canonical refresh overwrite a newer completed message', async () => {
    await store.open('parent', 'child')
    const source = EventSourceMock.instances[0]
    const stale = deferred()
    api.get.mockReturnValueOnce(stale.promise)
    source.emit('tool.completed', { toolCallId: 'tool' })
    expect(store.completedToolCallId).toBe('tool')
    const message = { id: 'answer', role: 'assistant', content: 'Neu' }
    session.messages = [message]
    source.emit('message.completed', { message })
    stale.resolve({ data: { ...session, messages: [{ ...message, content: 'Alt' }] } })
    await vi.waitFor(() => expect(api.get.mock.calls.filter(([url]) => url === '/sessions/child')).toHaveLength(3))
    expect(store.current.messages).toEqual([message])
  })

  it('keeps the parent stream and draft independent and routes child requests to the main lists', async () => {
    chat.current = { id: 'parent', messages: [] }
    chat.selectedSessionId = 'parent'
    chat.setDraft('Mein Entwurf')
    chat.watchRun('parent', 'parent-run', { status: 'running' })
    const parentSource = EventSourceMock.instances[0]
    await store.loadChildren('parent')
    await store.open('parent', 'child')
    const payload = { subagentSessionId: 'child', subagentTitle: child.title }
    const approval = { id: 'approval', sessionId: 'parent', runId: 'child-run', requestType: 'memory_change', payload }
    const question = { id: 'question', sessionId: 'parent', runId: 'child-run', payload, questions: [] }
    parentSource.emit('approval.created', { approval })
    parentSource.emit('question.created', { question })
    expect(useApprovalStore().forSession('parent')).toEqual([approval])
    expect(useQuestionStore().forSession('parent')).toEqual([question])
    expect(chat.activity.phase).not.toBe('waiting_input')
    parentSource.emit('message.delta', { delta: 'Elternantwort' })
    EventSourceMock.instances[1].emit('message.delta', { delta: 'Kindantwort' })
    expect(chat.streamContent).toBe('Elternantwort')
    expect(store.streamContent).toBe('Kindantwort')
    parentSource.emit('subagent.updated', { sessionId: 'child' })
    await vi.waitFor(() => expect(api.get.mock.calls.filter(([url]) => url.endsWith('/children'))).toHaveLength(2))
    store.close()
    expect(chat.current.id).toBe('parent')
    expect(chat.activeDraft).toBe('Mein Entwurf')
    expect(parentSource.close).not.toHaveBeenCalled()
  })

  it('never installs a child into the writable chat store for a direct session URL', async () => {
    const loaded = await chat.loadSession('child')
    expect(loaded.parentSessionId).toBe('parent')
    expect(chat.current).toBeNull()
    expect(api.post).not.toHaveBeenCalled()
    expect(EventSourceMock.instances).toHaveLength(0)
  })

  it('keeps child question provenance on initial discovery and forwarded events', async () => {
    const question = { id: 'question', sessionId: 'parent', runId: 'child-run', payload: { subagentSessionId: 'child', subagentTitle: child.title }, questions: [] }
    api.get.mockResolvedValueOnce({ data: [question] })
    const questions = useQuestionStore()
    await questions.fetchPending()
    expect(questions.forSession('parent')[0].payload).toEqual({ subagentSessionId: 'child', subagentTitle: child.title })
    questions.pending = []
    chat.current = { id: 'parent', messages: [] }
    chat.selectedSessionId = 'parent'
    chat.watchRun('parent', 'parent-run', { status: 'running' })
    EventSourceMock.instances[0].emit('question.created', { question })
    expect(chat.activity.phase).not.toBe('waiting_input')
    expect(questions.forSession('parent')[0].payload.subagentSessionId).toBe('child')
  })

  it('uses controlled generic status labels', () => {
    expect(subagentStatus(null)).toBe('Noch nicht gestartet')
    expect(subagentStatus({ status: 'failed' })).toBe('Fehlgeschlagen')
    expect(subagentStatus({ status: 'raw secret' })).not.toContain('secret')
  })

  it('keeps the spawning run on grouped tool entries even when tool IDs repeat', () => {
    const transcript = buildChatTranscript([
      { id: 'first', role: 'assistant', toolCalls: [{ id: 'task', name: 'task' }] },
      { id: 'first-result', role: 'tool', toolCallId: 'task', runId: 'run-1', content: '{}' },
      { id: 'user', role: 'user', content: 'Noch einmal' },
      { id: 'second', role: 'assistant', toolCalls: [{ id: 'task', name: 'task' }] },
      { id: 'second-result', role: 'tool', toolCallId: 'task', runId: 'run-2', content: '{}' }
    ])
    expect(transcript.messages.filter((message) => message.workHistory).map((message) => message.workHistory.entries[0].runId)).toEqual(['run-1', 'run-2'])
  })

  it('keeps parent activity running with initially fetched child requests and reused tool IDs', async () => {
    const approvals = useApprovalStore()
    const questions = useQuestionStore()
    const requests = ['child-1', 'child-2'].map((id) => ({
      id, sessionId: 'parent', runId: `${id}-run`, title: 'Datei freigeben',
      details: { toolCallId: 'same-call' }, toolCallId: 'same-call',
      questions: [{ id: 'q', question: 'Welche Datei?' }],
      payload: { subagentSessionId: id, subagentTitle: id }
    }))
    api.get.mockResolvedValueOnce({ data: requests }).mockResolvedValueOnce({ data: requests })
    await approvals.fetchPending()
    await questions.fetchPending()
    const activity = { phase: 'processing_tool', tool: 'task', toolCallId: 'same-call' }
    const transcript = buildChatTranscript([
      { id: 'user', role: 'user', content: 'Untersuchen' },
      { id: 'parent-message', role: 'assistant', toolCalls: [{ id: 'same-call', name: 'task' }] }
    ], approvals.forSession('parent'), { runId: 'parent-run', running: true, activeToolCallId: 'same-call', questions: questions.forSession('parent') })
    expect(transcript.unmatchedApprovals).toHaveLength(2)
    expect(transcript.unmatchedQuestions).toHaveLength(2)
    expect(transcript.messages[1].workHistory.entries[0]).toMatchObject({ active: true, approval: null, question: null })
    expect(agentRunPresentation({ runId: 'parent-run', running: true, activity, transcript }).label).toBe('Subagenten beauftragen')
    expect(agentRunPresentation({ runId: 'child-1-run', running: true, activity, transcript }).label).toBe('Freigabe erforderlich')
    const childTranscript = buildChatTranscript([
      { id: 'child-message', role: 'assistant', toolCalls: [{ id: 'same-call', name: 'read' }] }
    ], requests, { runId: 'child-1-run', running: true })
    expect(childTranscript.messages[0].workHistory.entries[0].approval.id).toBe('child-1')

    chat.current = { id: 'parent', messages: [] }
    chat.selectedSessionId = 'parent'
    chat.watchRun('parent', 'parent-run', { status: 'running' })
    const parentSource = EventSourceMock.instances[0]
    const phase = chat.activity.phase
    for (const name of ['approval.resolved', 'approval.expired', 'question.answered', 'question.rejected', 'question.expired']) {
      parentSource.emit(name, { subagentSessionId: 'child-1', subagentTitle: 'Dateien untersuchen' })
      expect(chat.running).toBe(true)
      expect(chat.activity.phase).toBe(phase)
      expect(chat.runId).toBe('parent-run')
    }
  })
})
