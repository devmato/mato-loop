import 'fake-indexeddb/auto'
import { createPinia, setActivePinia } from 'pinia'
import { watch } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { api } = vi.hoisted(() => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn()
  }
}))

vi.mock('src/boot/axios', () => ({ api }))
vi.mock('src/utils/notifications', () => ({ notifyError: vi.fn() }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm: vi.fn().mockResolvedValue(true) }) }))

import { useApprovalStore } from '../src/stores/approvals'
import { useChatStore } from '../src/features/chat/store'
import { useControlCenterStore } from '../src/stores/control-center'
import { useQuestionStore } from '../src/stores/questions'
import { useWorkspaceStore } from '../src/stores/workspace'
import { useGitActionStore } from '../src/features/coding/git-actions-store'
import { clearChatOutbox, loadChatOutbox, saveChatOutboxEntry } from '../src/features/chat/outbox-storage'
import * as outboxStorage from '../src/features/chat/outbox-storage'
import { buildAttachmentFormData, MAX_ATTACHMENT_DRAFT_BYTES } from '../src/features/chat/attachments'
import { withQueuedTranscriptMessages } from '../src/features/chat/transcript'
import { sessionSidebarStatus } from '../src/features/chat/session-groups'

const originalURL = globalThis.URL

function createStorage() {
  const values = new Map()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key)
  }
}

class EventSourceMock {
  constructor(url) {
    this.url = url
    this.listeners = new Map()
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener)
  }

  close() {}
}

function activeSession(chat, id = 'session-1') {
  chat.current = { id, messages: [] }
  chat.selectedSessionId = id
}

function attachment(name = 'datei.txt', size = 1) {
  const file = new Blob(['x'.repeat(Math.min(size, 10))], { type: 'text/plain' })
  Object.defineProperty(file, 'name', { value: name })
  Object.defineProperty(file, 'size', { value: size })
  return { id: `attachment-${name}`, file, kind: 'file', mimeType: 'text/plain', objectUrl: 'blob:preview' }
}

describe('chat message queue', () => {
  beforeEach(async () => {
    await clearChatOutbox('user-1')
    api.get.mockReset().mockImplementation(async (url) => ({ data: url === '/sessions/message-queue'
      ? { items: [] } : { id: url.split('/')[2], messages: [], activeRun: null } }))
    api.post.mockReset().mockRejectedValue(new Error('offline'))
    api.patch.mockReset()
    api.delete.mockReset()
    global.window = { localStorage: createStorage() }
    global.EventSource = EventSourceMock
    global.URL = { createObjectURL: vi.fn(() => 'blob:preview'), revokeObjectURL: vi.fn() }
    setActivePinia(createPinia())
    const chat = useChatStore()
    chat.setDraftOwner('user-1')
    await chat.outboxLoadPromise
    chat.cancelQueueDrain()
    vi.clearAllMocks()
  })

  afterEach(() => {
    useChatStore().cancelQueueDrain()
    delete global.window
    delete global.EventSource
    global.URL = originalURL
  })

  it('shows a pending send as active across navigation until delivery fails', async () => {
    const chat = useChatStore()
    activeSession(chat)
    let failSend, requestStarted
    const started = new Promise((resolve) => { requestStarted = resolve })
    api.post.mockImplementationOnce(() => new Promise((_resolve, reject) => {
      failSend = reject
      requestStarted()
    }))
    const status = () => sessionSidebarStatus({}, 0, 0, chat.sessionHasPendingSend('session-1'))?.kind || null
    const sending = chat.send('Hallo')
    const statuses = [status()]
    await started
    activeSession(chat, 'session-2')
    statuses.push(status())
    failSend(new Error('offline'))
    await sending
    statuses.push(status())
    expect(statuses).toEqual(['active', 'active', null])
  })

  it('persists text and attachments while a run is active', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    chat.setDraft('Später senden')
    chat.attachmentDrafts.sessionMessages['session-1'] = [attachment()]

    const entry = await chat.queueMessage('Später senden')

    expect(entry.content).toBe('Später senden')
    expect(chat.activeQueuedMessages).toHaveLength(1)
    expect(chat.activeQueuedMessages[0].attachments).toHaveLength(1)
    expect(chat.activeDraft).toBe('')
    expect(chat.activeAttachmentDraft).toEqual([])
    expect(global.URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview')
    expect(chat.queuedSendReady).toBe(true)
  })

  it('adds and removes the ordinary queued finish command without consuming the draft', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    chat.setDraft('Mein nächster Auftrag')
    chat.attachmentDrafts.sessionMessages['session-1'] = [attachment()]
    let queued = []
    api.post.mockImplementation(async (_url, payload) => {
      queued = [{ id: payload.run_id, sessionId: 'session-1', content: payload.content,
        attachments: [], serverQueued: true, commandPrompt: 'Review, Korrektur & Merge' }]
      return { data: { queued: true } }
    })
    api.get.mockImplementation(async () => ({ data: { items: queued } }))
    const git = useGitActionStore()
    await git.setFinishQueued(true)
    const entry = git.finishEntry
    const displayed = withQueuedTranscriptMessages([], chat.activeQueuedMessages)
    await git.setFinishQueued(false)
    expect([displayed.map(message => message.commandPrompt || message.content),
      api.delete.mock.calls.map(([url]) => url), Boolean(git.finishEntry), chat.activeQueuedMessages,
      chat.activeDraft, chat.activeAttachmentDraft.length]).toEqual([
      ['/git-finish'], [`/sessions/session-1/queue/${entry.id}`], false, [], 'Mein nächster Auftrag', 1
    ])
  })

  it('queues a prepared answer without consuming the current draft or attachments', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    chat.setDraft('Mein angefangener Text')
    chat.attachmentDrafts.sessionMessages['session-1'] = [attachment()]

    await chat.send('Verwende ERP-Kunde 123 – Acme.', { preserveDraft: true })

    expect(chat.activeQueuedMessages[0].content).toBe('Verwende ERP-Kunde 123 – Acme.')
    expect(chat.activeQueuedMessages[0].attachments).toEqual([])
    expect(chat.activeDraft).toBe('Mein angefangener Text')
    expect(chat.activeAttachmentDraft).toHaveLength(1)
    expect(global.URL.revokeObjectURL).not.toHaveBeenCalled()
  })

  it('does not publish a migrated queue after switching accounts', async () => {
    const chat = useChatStore()
    await saveChatOutboxEntry({ id: 'legacy', ownerUserId: 'user-1', sessionId: null,
      content: '/git-build Private task', attachments: [], queuedAt: new Date().toISOString() })
    let finishSave
    const save = vi.spyOn(outboxStorage, 'saveChatOutboxEntries')
      .mockImplementationOnce(() => new Promise((resolve) => { finishSave = resolve }))
    try {
      const loading = chat.loadQueuedMessages()
      await vi.waitFor(() => expect(finishSave).toBeTypeOf('function'))
      chat.setDraftOwner('user-2')
      await chat.outboxLoadPromise
      finishSave()
      await loading
      expect(Object.values(chat.queuedMessages).flat()).toEqual([])
    } finally { save.mockRestore() }
  })

  it('creates migrated follow-ups with clean delivery state', async () => {
    const chat = useChatStore()
    await saveChatOutboxEntry({ id: 'legacy', ownerUserId: 'user-1', sessionId: 'session-1',
      content: '/git-build Task', attachments: [], queuedAt: new Date().toISOString(),
      paused: true, sentMessageId: 'accepted-message' })
    await chat.loadQueuedMessages()
    chat.cancelQueueDrain()
    const finish = (await loadChatOutbox('user-1')).find((entry) => entry.afterRunId === 'legacy')
    expect([finish.paused, finish.sentMessageId]).toEqual([false, undefined])
  })

  it('preserves a new-chat draft when a Git shortcut creates the session', async () => {
    const chat = useChatStore()
    chat.setDraft('Keep my task')
    chat.attachmentDrafts.newSession.push(attachment('notes.txt'))
    api.post.mockImplementation(async (url) => ({ data: url === '/sessions'
      ? { session: { id: 'session-1', projectId: null } } : { queued: true } }))
    await useGitActionStore().run('git-review')
    expect([chat.activeDraft, chat.activeAttachmentDraft.map((item) => item.file.name),
      api.post.mock.calls.filter(([url]) => url.endsWith('/messages')).map(([, payload]) => payload.content)])
      .toEqual(['Keep my task', ['notes.txt'], ['/git-review']])
  })

  it('preserves voice markers through IndexedDB, failed sends, retry and restore', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    const audio = new File(['wav'], 'voice.wav', { type: 'audio/wav' })
    chat.addAttachmentFiles([audio])
    chat.addAttachmentFiles([audio], { voiceMessage: true })
    chat.addAttachmentFiles([audio], { voiceMessage: true })
    const entry = await chat.queueMessage('')
    expect((await loadChatOutbox('user-1'))[0].attachments.map((item) => item.voiceMessage))
      .toEqual([false, true, true])

    setActivePinia(createPinia())
    const restored = useChatStore()
    restored.setDraftOwner('user-1')
    await restored.outboxLoadPromise
    restored.cancelQueueDrain()
    activeSession(restored)
    api.post.mockRejectedValueOnce({ response: { status: 422 } })
    await restored.drainOutbox()
    expect(JSON.parse(api.post.mock.calls.at(-1)[1].get('payload')).voiceAttachmentIndices).toEqual([1, 2])
    expect(restored.activeQueuedMessages[0].attachments[1].voiceMessage).toBe(true)

    restored.setAudioDictation({ files: [audio] }, 'session-1')
    await restored.restoreQueuedMessage(entry.id)
    expect(restored.getAudioDictation('session-1').files).toHaveLength(1)
    expect(restored.activeAttachmentDraft.map((item) => item.voiceMessage)).toEqual([false, true, true])
    expect(restored.activeAttachmentDraft.every((item) => item.objectUrl === 'blob:preview')).toBe(true)
    const form = buildAttachmentFormData({ voiceAttachmentIndices: [0] }, restored.activeAttachmentDraft.slice(1))
    expect(JSON.parse(form.get('payload')).voiceAttachmentIndices).toEqual([0, 1])
    vi.spyOn(restored, 'watchRun').mockImplementation(() => {})
    api.post.mockResolvedValueOnce({ data: { message: { id: 'sent' }, runId: 'run-1' } })
    await restored.send('')
    expect(JSON.parse(api.post.mock.calls.at(-1)[1].get('payload')).voiceAttachmentIndices).toEqual([1, 2])
    expect(restored.activeQueuedMessages).toEqual([])
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview')
  })

  it('retains rejected recordings when sending the first message creates a session', async () => {
    const chat = useChatStore()
    const audio = new File(['wav'], 'rejected.wav', { type: 'audio/wav' })
    chat.setAudioDictation({ files: [audio] })
    chat.drafts.newSession.options.agentProfileId = 'agent-b'
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})
    vi.spyOn(useControlCenterStore(), 'attachCreatedSession').mockResolvedValue(null)
    api.post.mockResolvedValue({
      data: { session: { id: 'session-1' }, message: { id: 'sent' }, runId: 'run-1' }
    })
    await chat.startSession('Text')
    expect(chat.activeAgentProfileId).toBe('agent-b')
    expect(chat.getAudioDictation('session-1').files[0]).toBe(audio)
    expect(chat.getAudioDictation()).toBeNull()
  })

  it('keeps an idle submission queued until the server accepts it', async () => {
    let resolveSend
    api.post.mockReturnValue(new Promise((resolve) => { resolveSend = resolve }))
    const chat = useChatStore()
    activeSession(chat)
    chat.current.agentProfileId = 'agent-a'
    await chat.updateActive({ agent_profile_id: 'agent-b' }, { draftOnly: true })
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})
    chat.attachmentDrafts.sessionMessages['session-1'] = [attachment()]
    const displayed = []
    const stopWatching = watch(() => withQueuedTranscriptMessages(
      chat.current.messages, chat.activeQueuedMessages, chat.queueSendingEntryId
    ), (messages) => displayed.push(messages.filter((message) => message.content === 'Jetzt senden').length))

    const sending = chat.send('Jetzt senden')

    expect(withQueuedTranscriptMessages(
      chat.current.messages, chat.activeQueuedMessages, chat.queueSendingEntryId, chat.activePendingMessage?.id
    )).toMatchObject([{ content: 'Jetzt senden', attachments: [{ name: 'datei.txt' }], deliveryStatus: 'sending' }])
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledOnce())
    expect(chat.activeQueuedMessages.map((entry) => entry.content)).toEqual(['Jetzt senden'])
    expect(chat.queueSendingEntryId).toBe(chat.activeQueuedMessages[0].id)
    expect(chat.current.messages).toEqual([])

    resolveSend({
      data: {
        runId: 'run-1',
        message: { id: 'message-1', role: 'user', content: 'Jetzt senden' }
      }
    })
    api.get.mockImplementation(async (url) => ({ data: url === '/sessions/message-queue' ? { items: [] } : {
      id: 'session-1', messages: [{ id: 'message-1', requestId: chat.activeQueuedMessages[0]?.id, role: 'user', content: 'Jetzt senden' }], activeRun: null
    } }))
    await sending
    stopWatching()

    expect(chat.activeAgentProfileId).toBe('agent-b')
    expect(chat.activeQueuedMessages).toEqual([])
    expect(displayed.every((count) => count === 1)).toBe(true)
    expect(chat.current.messages).toEqual([
      { id: 'message-1', requestId: expect.any(String), role: 'user', content: 'Jetzt senden' }
    ])
  })

  it('persists the first queued message with a reactive session profile', async () => {
    const chat = useChatStore()
    const profile = { agentModels: { plan: { modelId: 'plan-model', reasoningEffort: 'high' } } }
    chat.drafts.newSession.options.sessionProfile = profile
    await chat.queueMessage('Test')
    expect((await loadChatOutbox('user-1'))[0]?.options.sessionProfile).toEqual(profile)
  })

  it('restores the outbox with its stable id and file after a reload', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    chat.attachmentDrafts.sessionMessages['session-1'] = [attachment('beleg.txt')]
    const queued = await chat.queueMessage('Bleibt erhalten')

    setActivePinia(createPinia())
    const restored = useChatStore()
    restored.setDraftOwner('user-1')
    await restored.outboxLoadPromise
    restored.cancelQueueDrain()
    activeSession(restored)

    expect(restored.activeQueuedMessages).toHaveLength(1)
    expect(restored.activeQueuedMessages[0]).toMatchObject({
      id: queued.id,
      content: 'Bleibt erhalten',
      sessionId: 'session-1'
    })
    expect(restored.activeQueuedMessages[0].attachments[0]).toMatchObject({
      name: 'beleg.txt',
      file: expect.any(Blob)
    })
  })

  it('keeps each queued profile through reload and retries while a later selection remains pending', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.current.agentProfileId = 'agent-a'
    chat.running = true
    await chat.updateActive({ agent_profile_id: 'agent-b' })
    const first = await chat.queueMessage('Erste')
    await chat.updateActive({ agent_profile_id: 'agent-a' })
    const second = await chat.queueMessage('Zweite')
    await chat.updateActive({ agent_profile_id: 'agent-b' })

    setActivePinia(createPinia())
    const restored = useChatStore()
    restored.setDraftOwner('user-1')
    await restored.outboxLoadPromise
    restored.cancelQueueDrain()
    activeSession(restored)
    restored.current.agentProfileId = 'agent-a'
    vi.spyOn(restored, 'refreshSession').mockResolvedValue()
    vi.spyOn(restored, 'watchRun').mockImplementation(() => {})
    expect(restored.activeQueuedMessages.map((entry) => entry.agentProfileId)).toEqual(['agent-b', 'agent-a'])
    expect(restored.activeAgentProfileId).toBe('agent-b')

    api.post.mockRejectedValueOnce({ response: { status: 409 } })
    await restored.drainOutbox()
    expect(restored.activeQueuedMessages[0].paused).toBe(true)
    expect(api.post.mock.calls[0][1].agent_profile_id).toBe('agent-b')
    restored.cancelQueueDrain()
    api.post.mockResolvedValueOnce({ data: {
      message: { id: 'message-1' }, runId: first.id, agentProfileId: 'agent-b'
    } })
    restored.activeQueuedMessages[0].paused = false
    await restored.drainOutbox()
    restored.cancelQueueDrain()
    expect(restored.current.agentProfileId).toBe('agent-a')
    expect(restored.activeAgentProfileId).toBe('agent-b')
    expect(restored.activeQueuedMessages[0].id).toBe(second.id)

    restored.running = false
    restored.runId = null
    api.post.mockResolvedValueOnce({ data: {
      message: { id: 'message-2' }, runId: second.id, agentProfileId: 'agent-a'
    } })
    await restored.drainOutbox()
    expect(api.post.mock.calls.at(-1)[1].agent_profile_id).toBe('agent-a')
    expect(restored.current.agentProfileId).toBe('agent-a')
    expect(restored.activeAgentProfileId).toBe('agent-b')
  })

  it('sends the queued profile when the session is no longer selected', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.current.agentProfileId = 'agent-a'
    chat.running = true
    await chat.updateActive({ agent_profile_id: 'agent-b' })
    const entry = await chat.queueMessage('Im Hintergrund')
    chat.current = { id: 'session-2', messages: [] }
    chat.selectedSessionId = 'session-2'
    vi.spyOn(chat, 'refreshSessions').mockResolvedValue([])
    api.get.mockResolvedValue({ data: { activeRun: null } })
    api.post.mockResolvedValue({ data: {
      message: { id: 'message-1' }, runId: entry.id, agentProfileId: 'agent-b'
    } })

    await chat.drainOutbox()

    expect(api.post).toHaveBeenCalledWith('/sessions/session-1/messages', {
      content: 'Im Hintergrund', run_id: entry.id, agent_profile_id: 'agent-b', enqueue: true
    }, { suppressErrorToast: true })
    expect(chat.drafts.sessionAgentProfiles['session-1']).toBe('agent-b')
  })

  it('queues the first message again immediately after undoing it', async () => {
    const chat = useChatStore()
    const message = { id: 'message-1', role: 'user', content: 'Noch einmal', attachments: [] }
    chat.current = {
      id: 'session-1',
      messages: [message],
      workspaceContext: { connected: true, operations: ['filesystem.list'] }
    }
    chat.selectedSessionId = 'session-1'
    api.get.mockResolvedValue({ data: { id: 'session-1', messages: [], activeRun: null } })
    api.post.mockResolvedValue({
      data: { draft: { content: message.content } }
    })

    await chat.undoUserMessage(message)
    const queued = await chat.queueMessage(chat.activeDraft)

    expect(queued).not.toBeNull()
    expect(queued.sessionId).toBe('session-1')

    setActivePinia(createPinia())
    const restored = useChatStore()
    restored.setDraftOwner('user-1')
    await restored.outboxLoadPromise
    restored.cancelQueueDrain()
    activeSession(restored)

    expect(restored.activeQueuedMessages).toHaveLength(1)
    expect(restored.activeQueuedMessages[0]).toMatchObject({
      id: queued.id,
      content: 'Noch einmal',
      sessionId: 'session-1'
    })
  })

  it('persists the queue-time active file snapshot and sends it unchanged', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    const controlCenter = useControlCenterStore()
    controlCenter.sessionId = 'session-1'
    await controlCenter.setOpen(true)
    controlCenter.tabs = [{ appId: 'files', pinned: false }]
    controlCenter.activeAppId = 'files'
    const workspace = useWorkspaceStore()
    workspace.sessionId = 'session-1'
    workspace.scope = 'session'
    workspace.rootIdentity = 'session-1'
    workspace.openEntry({ kind: 'file', name: 'plan.md', path: 'plan.md' })
    workspace.filePreviews['plan.md'] = { revision: 'a'.repeat(64) }

    const entry = await chat.queueMessage('Mit Datei')
    workspace.filePreviews['plan.md'].revision = 'b'.repeat(64)
    chat.running = false
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})
    api.post.mockResolvedValue({ data: { message: { id: 'message-1' }, runId: 'run-1' } })
    await chat.drainOutbox()

    const snapshot = {
      root: 'session',
      root_identity: 'session-1',
      path: 'plan.md',
      sha256: 'a'.repeat(64),
      dirty: false
    }
    expect(entry.activeFile).toEqual(snapshot)
    expect(api.post).toHaveBeenCalledWith('/sessions/session-1/messages', {
      content: 'Mit Datei',
      run_id: entry.id,
      enqueue: true,
      active_file: snapshot
    }, { suppressErrorToast: true })
  })

  it('does not queue a sibling session file with the current chat message', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    const controlCenter = useControlCenterStore()
    controlCenter.sessionId = 'session-1'
    await controlCenter.setOpen(true)
    controlCenter.tabs = [{ appId: 'files', pinned: false }]
    controlCenter.activeAppId = 'files'
    const workspace = useWorkspaceStore()
    workspace.sessionId = 'session-1'
    workspace.scope = 'session'
    workspace.rootIdentity = 'session-2'
    workspace.openEntry({ kind: 'file', name: 'plan.md', path: 'plan.md' })
    workspace.filePreviews['plan.md'] = { revision: 'a'.repeat(64) }

    const entry = await chat.queueMessage('Ohne fremde Datei')

    expect(entry.activeFile).toBeNull()
    expect((await loadChatOutbox('user-1'))[0].activeFile).toBeNull()
  })

  it('hands messages to the backend while approvals and questions are pending', async () => {
    const chat = useChatStore()
    const approvals = useApprovalStore()
    const questions = useQuestionStore()
    activeSession(chat)

    approvals.pending = [{ id: 'approval-1', sessionId: 'session-1' }]
    await chat.send('Nachricht')

    expect(api.post).toHaveBeenCalledWith('/sessions/session-1/messages', expect.objectContaining({ enqueue: true }), expect.anything())
    expect(chat.activeQueuedMessages.map((entry) => entry.content)).toEqual(['Nachricht'])
    expect(chat.activity.phase).toBe('idle')
    expect(chat.queuedSendReady).toBe(false)

    approvals.pending = []
    questions.pending = [{ id: 'question-1', sessionId: 'session-1' }]
    expect(chat.queuedSendReady).toBe(false)

    questions.pending = []
    expect(chat.messageQueueActive).toBe(true)
    expect(chat.queuedSendReady).toBe(false)
  })

  it('drains messages in FIFO order and keeps a newer draft', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    const first = await chat.queueMessage('Zuerst')
    const second = await chat.queueMessage('Danach')
    chat.running = false
    chat.setDraft('Neuer Entwurf')
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})
    api.post.mockResolvedValue({ data: { message: { id: 'message-1' }, runId: 'run-1' } })

    await chat.drainOutbox()
    chat.running = false
    await chat.drainOutbox()

    expect(api.post.mock.calls.map(([, payload]) => payload)).toEqual([
      { content: 'Zuerst', run_id: first.id, enqueue: true },
      { content: 'Danach', run_id: second.id, enqueue: true }
    ])
    expect(chat.activeQueuedMessages).toEqual([])
    expect(chat.activeDraft).toBe('Neuer Entwurf')
  })

  it('retains backend acceptance when reconciliation fails and never uploads it again', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    await chat.queueMessage('Zuerst')
    api.post.mockResolvedValue({ data: { queued: true } })
    api.get.mockRejectedValue(new Error('offline'))
    await chat.drainOutbox()
    await chat.drainOutbox()
    expect([api.post.mock.calls.length, chat.activeQueuedMessages[0].serverQueued, chat.activeQueuedMessages[0].paused])
      .toEqual([1, true, false])
  })

  it('saves a direct message before adding it to the active run, ahead of queued messages', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.watchRun('session-1', 'run-1', { status: 'running' })
    await chat.queueMessage('Später')
    chat.drafts.sessionAgentProfiles['session-1'] = 'agent-b'
    api.post.mockImplementation(async (_url, payload) => {
      expect((await loadChatOutbox('user-1')).map((entry) => entry.content)).toEqual([
        'Jetzt', 'Später'
      ])
      return { data: { message: { id: payload.message_id, role: 'user', content: 'Jetzt', continuationRunId: 'run-1' }, runId: 'run-1' } }
    })

    await chat.send('Jetzt', { interrupt: true })

    expect(api.post).toHaveBeenCalledTimes(1)
    expect(api.post.mock.calls[0][0]).toBe('/sessions/session-1/runs/run-1/messages')
    expect(api.post.mock.calls[0][1]).toMatchObject({ content: 'Jetzt', message_id: expect.any(String), agent_profile_id: 'agent-b' })
    chat.eventSource.listeners.get('run.profile.updated')({ data: JSON.stringify({ agentProfileId: 'agent-b', modelName: 'New model' }) })
    expect([chat.runAgentProfileId, chat.current.agentProfileId, chat.streamModelName]).toEqual(['agent-b', 'agent-b', 'New model'])
    chat.eventSource.listeners.get('run.profile.updated')({ data: JSON.stringify({ agentProfileId: 'agent-a', modelName: 'Older queued model' }) })
    expect(chat.activeAgentProfileId).toBe('agent-b')
    expect(chat.eventSource.url).toBe('/api/sessions/session-1/runs/run-1/events')
    expect(chat.current.messages.map((message) => message.content)).toEqual(['Jetzt'])
    expect(chat.activeQueuedMessages.map((entry) => entry.content)).toEqual(['Später'])
  })

  it('keeps direct-message priority after a reload and retains the message when appending fails', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.watchRun('session-1', 'run-1', { status: 'running' })
    await chat.queueMessage('Später')
    api.post.mockRejectedValue(new Error('Append failed'))

    await chat.send('Jetzt', { interrupt: true })
    await vi.waitFor(() => expect(chat.running).toBe(true))

    expect(api.post).toHaveBeenCalledTimes(1)
    expect(chat.activeQueuedMessages.map((entry) => entry.content)).toEqual(['Jetzt', 'Später'])
    expect(chat.activeQueuedMessages[0].paused).toBe(true)
    expect(chat.activeQueuedMessages[0].continuationRunId).toBe('run-1')
    setActivePinia(createPinia())
    const restored = useChatStore()
    restored.setDraftOwner('user-1')
    await restored.outboxLoadPromise
    restored.cancelQueueDrain()
    activeSession(restored)
    expect(restored.activeQueuedMessages.map((entry) => entry.content)).toEqual(['Jetzt', 'Später'])
    api.post.mockResolvedValueOnce({ data: {
      message: { id: restored.activeQueuedMessages[0].id, role: 'user', content: 'Jetzt', continuationRunId: 'run-1' },
      runId: 'run-1'
    } })
    await restored.resumeQueuedSend()
    await vi.waitFor(() => expect(restored.activeQueuedMessages.map((entry) => entry.content)).toEqual(['Später']))
    expect(api.post.mock.calls[1][0]).toBe('/sessions/session-1/runs/run-1/messages')
    restored.cancelQueueDrain()
  })

  it('places a direct message before a partial answer that arrived while it was submitting', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.watchRun('session-1', 'run-1', { status: 'running' })
    let resolveSend
    api.post.mockReturnValue(new Promise((resolve) => { resolveSend = resolve }))
    const sending = chat.send('Korrektur', { interrupt: true })
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledOnce())
    chat.current.messages.push({
      id: 'partial', role: 'assistant', runId: 'run-1', continuationSegment: true,
      content: 'Zwischenstand', createdAt: '2026-01-01T00:00:02Z'
    })
    resolveSend({ data: {
      message: { id: api.post.mock.calls[0][1].message_id, role: 'user',
        content: 'Korrektur', createdAt: '2026-01-01T00:00:01Z', continuationRunId: 'run-1' },
      runId: 'run-1'
    } })
    await sending
    expect(chat.current.messages.map((message) => message.content)).toEqual(['Korrektur', 'Zwischenstand'])
  })

  it('does not cancel a run when the direct message cannot be saved', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.watchRun('session-1', 'run-1', { status: 'running' })
    chat.setDraft('Nicht verlieren')
    vi.stubGlobal('indexedDB', undefined)
    try {
      await chat.send('Nicht verlieren', { interrupt: true })

      expect(api.post).not.toHaveBeenCalled()
      expect(chat.running).toBe(true)
      expect(chat.activeDraft).toBe('Nicht verlieren')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('keeps an unaccepted direct message when its run has already finished', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.watchRun('session-1', 'run-1', { status: 'running' })
    api.post.mockRejectedValueOnce({ response: { status: 409, data: { error: { code: 'session.runInactive' } } } })

    await chat.send('Noch wichtig', { interrupt: true })

    chat.cancelQueueDrain()
    const pending = chat.activeQueuedMessages[0]
    expect(pending.id).toBeTruthy()
    expect(pending.continuationRunId).toBeUndefined()
    expect((await loadChatOutbox('user-1'))[0].id).toBe(pending.id)
    chat.clearRunTracking()
    chat.running = false
    api.post.mockResolvedValueOnce({ data: {
      message: { id: 'next-message', role: 'user', content: 'Noch wichtig' }, runId: 'run-2'
    } })

    await chat.drainOutbox()

    expect(api.post.mock.calls.map(([url]) => url)).toEqual([
      '/sessions/session-1/runs/run-1/messages', '/sessions/session-1/messages'
    ])
    expect(api.post.mock.calls[1][1].run_id).toBe(pending.id)
  })

  it.each(['run.completed', 'run.failed', 'run.cancelled', 'reconnect', 'navigation', 'session-switch'])(
    'keeps canonical outputs closed until the user opens them after %s', async (terminal) => {
      const chat = useChatStore()
      const sessionId = '550e8400-e29b-41d4-a716-446655440000'
      const runId = '550e8400-e29b-41d4-a716-446655440001'
      activeSession(chat, sessionId)
      api.get.mockResolvedValue({ data: [] })
      vi.spyOn(chat, 'refreshSessions').mockResolvedValue()
      const controlCenter = useControlCenterStore()
      const open = vi.spyOn(controlCenter, 'openFile').mockResolvedValue('file-tab')
      const final = {
        id: sessionId, activeRun: null,
        messages: [
          { id: 'old', role: 'tool', runId: 'other-run', attachments: [] },
          { id: 'hidden', role: 'tool', runId, attachments: [], fileActions: [] },
          { id: 'final', role: 'tool', runId, attachments: [{
            id: 'output', kind: 'file', filename: 'final.stl', mimeType: 'model/stl',
            fileRootId: sessionId, fileId: '550e8400-e29b-41d4-a716-446655440002',
            fileRevision: 3, filePath: 'final.stl', sizeBytes: 10
          }] }
        ]
      }
      const attachment = final.messages[2].attachments[0]
      final.messages[0].attachments = [{ ...attachment, filePath: 'previous-run.stl' }]
      final.messages.push({
        id: 'second', role: 'tool', runId,
        attachments: [attachment, { ...attachment, filePath: 'second.stl' }]
      })
      let resolveFinal
      const refresh = vi.spyOn(chat, 'refreshSession').mockResolvedValue({ ...final, messages: [] })
      chat.watchRun(sessionId, runId)
      const source = chat.eventSource
      source.listeners.get('tool.completed')({ data: JSON.stringify({ tool: 'render_openscad', toolCallId: 'tool-1' }) })
      await Promise.resolve()
      expect(open).not.toHaveBeenCalled()
      refresh.mockImplementation(() => new Promise((resolve) => { resolveFinal = resolve }))
      if (terminal === 'reconnect') {
        api.get.mockResolvedValue({ data: { status: 'cancelled' } })
        await source.onerror()
      } else source.listeners.get(terminal.startsWith('run.') ? terminal : 'run.completed')()
      expect(open).not.toHaveBeenCalled()
      if (terminal === 'navigation') useWorkspaceStore().viewVersion += 1
      if (terminal === 'session-switch') activeSession(chat, 'another-session')
      resolveFinal(final)
       for (let tick = 0; tick < 5; tick++) await Promise.resolve()
       expect(open).not.toHaveBeenCalled()
    }
  )

  it('keeps and pauses a message when sending fails', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    await chat.queueMessage('Fehlschlag')
    chat.running = false
    api.post.mockRejectedValue(new Error('offline'))

    await chat.drainOutbox()

    expect(chat.activeQueuedMessages[0].content).toBe('Fehlschlag')
    expect(chat.queuedSendPaused).toBe(true)
    const firstRunId = api.post.mock.calls[0][1].run_id
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})
    api.post.mockResolvedValue({ data: { message: { id: 'message-1' }, runId: firstRunId } })
    await chat.resumeQueuedSend()
    chat.cancelQueueDrain()
    await chat.drainOutbox()

    expect(api.post.mock.calls[1][1].run_id).toBe(firstRunId)
    expect(chat.activeQueuedMessages).toEqual([])
  })

  it('waits without pausing when the server still has an active run', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    await chat.queueMessage('Zu früh')
    chat.running = false
    api.post.mockRejectedValue({
      response: { status: 409, data: { error: { code: 'session.runAlreadyActive' } } }
    })

    await chat.drainOutbox()

    expect(chat.activeQueuedMessages).toHaveLength(1)
    expect(chat.queuedSendPaused).toBe(false)
  })

  it('restores a failed send as paused after a reload', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    await chat.queueMessage('Nicht automatisch wiederholen')
    chat.running = false
    api.post.mockRejectedValue(new Error('offline'))
    await chat.drainOutbox()

    setActivePinia(createPinia())
    const restored = useChatStore()
    restored.setDraftOwner('user-1')
    await restored.outboxLoadPromise
    restored.cancelQueueDrain()
    activeSession(restored)

    expect(restored.activeQueuedMessages).toHaveLength(1)
    expect(restored.queuedSendPaused).toBe(true)
  })

  it('accepts only one submission while IndexedDB is saving', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true

    const first = chat.send('Nur einmal')
    const second = chat.send('Nicht doppelt')
    await Promise.all([first, second])

    expect(chat.activeQueuedMessages.map((entry) => entry.content)).toEqual(['Nur einmal'])
  })

  it('keeps the draft and attachments without sending when local saving fails', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.setDraft('Nicht verlieren')
    chat.attachmentDrafts.sessionMessages['session-1'] = [attachment()]
    const indexedDB = globalThis.indexedDB
    globalThis.indexedDB = undefined
    try {
      await chat.send('Nicht verlieren')
      expect([chat.activeQueuedMessages, chat.activeDraft, chat.activeAttachmentDraft.length, api.post.mock.calls])
        .toEqual([[], 'Nicht verlieren', 1, []])
    } finally {
      globalThis.indexedDB = indexedDB
    }
  })

  it('removes a queued message from memory and IndexedDB', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    const entry = await chat.queueMessage('Verwerfen')

    await chat.removeQueuedMessage(entry.id)

    expect(chat.activeQueuedMessages).toEqual([])
    setActivePinia(createPinia())
    const restored = useChatStore()
    restored.setDraftOwner('user-1')
    await restored.outboxLoadPromise
    restored.cancelQueueDrain()
    activeSession(restored)
    expect(restored.activeQueuedMessages).toEqual([])
  })

  it('does not send a message while its local deletion is pending', async () => {
    const chat = useChatStore()
    activeSession(chat)
    const entry = await chat.queueMessage('/git-finish')
    let finishDeletion
    const deletion = vi.spyOn(outboxStorage, 'deleteChatOutboxEntries')
      .mockReturnValueOnce(new Promise(resolve => { finishDeletion = resolve }))
    try {
      const removing = chat.removeQueuedMessage(entry.id)
      await chat.drainNextOutboxEntry()
      finishDeletion()
      await removing
      expect(api.post).not.toHaveBeenCalled()
    } finally { deletion.mockRestore() }
  })

  it('restores a queued message with attachments to the composer', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    chat.attachmentDrafts.sessionMessages['session-1'] = [attachment('entwurf.txt')]
    const first = await chat.queueMessage('Bearbeiten')
    await chat.queueMessage('Danach')

    await chat.restoreQueuedMessage(first.id)

    expect(chat.activeDraft).toBe('Bearbeiten')
    expect(chat.activeAttachmentDraft).toHaveLength(1)
    expect(chat.activeAttachmentDraft[0].file.name).toBe('entwurf.txt')
    expect(chat.activeQueuedMessages.map((entry) => entry.content)).toEqual(['Danach'])
    chat.cancelQueueDrain()
    setActivePinia(createPinia())
    const restored = useChatStore()
    restored.setDraftOwner('user-1')
    await restored.outboxLoadPromise
    restored.cancelQueueDrain()
    activeSession(restored)
    expect(restored.activeQueuedMessages.map((entry) => entry.content)).toEqual(['Danach'])
  })

  it('restores a message while its first local save is pending and never submits it', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.setDraft('Bearbeiten')
    const sending = chat.send('Bearbeiten')
    const undoing = chat.restoreQueuedMessage(chat.activePendingMessage.id)
    await Promise.all([sending, undoing])
    expect([api.post.mock.calls.length, chat.activeDraft, chat.activeQueuedMessages]).toEqual([0, 'Bearbeiten', []])
  })

  it.each(['delete', 'attachment'])('undoes the admitted message if the queue hands it off before %s', async (transition) => {
    const chat = useChatStore()
    const sessionId = 'dc51d2b0-ea52-46df-9eb5-62b73031f74d'
    const messageId = '22222222-2222-4222-8222-222222222222'
    const attachmentId = '33333333-3333-4333-8333-333333333333'
    activeSession(chat, sessionId)
    const entry = { id: 'request', sessionId, serverQueued: true, content: 'Bearbeiten',
      attachments: transition === 'attachment' ? [{ name: 'datei.txt', kind: 'file' }] : [] }
    const message = { id: messageId, role: 'user', content: entry.content,
      attachments: transition === 'attachment' ? [{ filename: 'datei.txt', url: `/api/sessions/${sessionId}/messages/${messageId}/attachments/${attachmentId}` }] : [] }
    const handoff = { response: { status: 409, data: { error: {
      code: 'session.runAlreadyActive', params: { messageId: message.id }
    } } } }
    chat.queuedMessages[sessionId] = [entry]
    api.delete.mockRejectedValueOnce(handoff)
    let undone = false
    api.get.mockImplementation(async (url) => {
      if (url.includes('/queue/')) throw handoff
      if (url.includes('/attachments/')) return { data: new Blob(['saved upload']) }
      if (url === `/sessions/${sessionId}`) return { data: { id: sessionId, messages: undone ? [] : [message] } }
      return { data: { items: [] } }
    })
    api.post.mockImplementation(async () => {
      undone = true
      return { data: { draft: { content: entry.content } } }
    })

    await chat.restoreQueuedMessage(entry.id)
    await vi.waitFor(() => expect(chat.activeQueuedMessages).toEqual([]))
    expect([api.post.mock.calls[0]?.[0], chat.current.messages, chat.activeDraft, chat.activeAttachmentDraft[0]?.file.name])
      .toEqual([`/sessions/${sessionId}/messages/${messageId}/undo`, [], 'Bearbeiten', transition === 'attachment' ? 'datei.txt' : undefined])
  })

  it('accepts undo during the upload and removes the server-queued message after acknowledgement', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    const entry = await chat.queueMessage('Bearbeiten')
    let finishSend
    api.post.mockReturnValueOnce(new Promise((resolve) => { finishSend = resolve }))
    api.get.mockImplementation(async () => ({ data: { items: [{ ...entry, serverQueued: true }] } }))
    const sending = chat.drainOutbox()
    const undoing = chat.restoreQueuedMessage(entry.id)
    finishSend({ data: { queued: true } })
    await Promise.all([sending, undoing])
    expect([api.delete.mock.calls[0]?.[0], chat.activeDraft, chat.running, chat.activeQueuedMessages])
      .toEqual([`/sessions/session-1/queue/${entry.id}`, 'Bearbeiten', true, []])
  })

  it('undoes the first message when the send and session creation are still pending', async () => {
    const chat = useChatStore()
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})
    vi.spyOn(useControlCenterStore(), 'attachCreatedSession').mockResolvedValue(null)
    const entry = await chat.queueMessage('Erster Entwurf')
    let finishSend
    api.post.mockImplementation((url) => {
      if (url === '/sessions') return new Promise((resolve) => { finishSend = resolve })
      if (url.endsWith('/undo')) return Promise.resolve({ data: { draft: { content: entry.content } } })
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
    const sending = chat.drainOutbox()
    const undoing = chat.restoreQueuedMessage(entry.id)
    finishSend({ data: { session: { id: 'session-1' }, runId: entry.id,
      message: { id: 'sent', role: 'user', content: entry.content } } })
    await Promise.all([sending, undoing])
    expect([api.post.mock.calls.map(([url]) => url).filter((url) => url.endsWith('/undo')),
      chat.current.id, chat.current.messages, chat.activeDraft])
      .toEqual([['/sessions/session-1/messages/sent/undo'], 'session-1', [], 'Erster Entwurf'])
  })

  it('restores server uploads and ignores a queue response started before removal', async () => {
    let resolveStale
    const chat = useChatStore()
    activeSession(chat)
    const entry = { id: 'server-request', sessionId: 'session-1', ownerUserId: 'user-1', content: 'Bearbeiten',
      queuedAt: new Date().toISOString(), queuePosition: 1, paused: false, serverQueued: true,
      attachments: [{ name: 'entwurf.txt', mimeType: 'text/plain', kind: 'file' }] }
    api.get.mockImplementation(async (url) => ({ data: url.endsWith('/attachments/0')
      ? new Blob(['saved upload']) : { items: [entry] } }))
    await chat.refreshServerQueue()
    api.get.mockImplementationOnce(() => new Promise((resolve) => { resolveStale = resolve }))
    const stale = chat.refreshServerQueue()
    api.delete.mockResolvedValue({})
    await chat.restoreQueuedMessage(entry.id)
    resolveStale({ data: { items: [entry] } })
    await stale
    expect([chat.activeDraft, await chat.activeAttachmentDraft[0].file.text(), chat.activeQueuedMessages])
      .toEqual(['Bearbeiten', 'saved upload', []])
  })

  it.each(['first', 'continuation'])('undoes a committed %s send after its response is lost', async (mode) => {
    const chat = useChatStore()
    if (mode === 'continuation') {
      activeSession(chat)
      chat.runSessionId = 'session-1'
      chat.runId = 'active-run'
    }
    const entry = await chat.queueMessage('Bearbeiten', { interrupt: mode === 'continuation' })
    const messageId = mode === 'first' ? 'committed-message' : entry.id
    const route = `/sessions/session-1/messages/${messageId}/undo`
    let failSend
    let undone = false
    api.get.mockImplementation(async (url) => ({ data: url.startsWith('/sessions/message-submissions/')
      ? { sessionId: 'session-1', messageId }
      : url === '/sessions/session-1' ? { id: 'session-1', messages: undone ? [] : [
          { id: messageId, role: 'user', content: entry.content }
        ] } : { items: [] } }))
    api.post.mockImplementation((url) => {
      if (url === route) {
        undone = true
        return Promise.resolve({ data: { draft: { content: entry.content } } })
      }
      if (url === '/sessions/defaults') return Promise.resolve({ data: {} })
      return new Promise((_, reject) => { failSend = reject })
    })
    const sending = chat.drainOutbox()
    const restoring = chat.restoreQueuedMessage(entry.id)
    failSend(new Error('Connection lost after commit'))
    await Promise.all([sending, restoring])
    expect({ undone, draft: chat.drafts.sessionMessages['session-1'], stored: await loadChatOutbox('user-1') })
      .toEqual({ undone: true, draft: 'Bearbeiten', stored: [] })
  })

  it.each(['offline', 'missing'])('keeps the local message paused when removal is unconfirmed (%s)', async (state) => {
    const chat = useChatStore()
    activeSession(chat)
    const entry = await chat.queueMessage('Nur lokal gespeichert')
    let failSend
    api.post.mockImplementationOnce(() => new Promise((_, reject) => { failSend = reject }))
    api.delete.mockRejectedValue(state === 'offline' ? new Error('offline') : {
      response: { status: 404, data: { error: { code: 'session.messageNotFound' } } }
    })
    const sending = chat.drainOutbox()
    const restoring = chat.restoreQueuedMessage(entry.id)
    failSend(new Error('offline'))
    const [, restored] = await Promise.all([sending, restoring])
    expect([restored, await loadChatOutbox('user-1')]).toMatchObject([
      null, [{ id: entry.id, content: entry.content, paused: true, deliveryUnconfirmed: true }]
    ])
  })

  it('counts an admitted attachment only once when undo moves it back to the draft', async () => {
    const chat = useChatStore()
    activeSession(chat)
    const entry = { id: 'request', sessionId: 'session-1', serverQueued: true, content: 'Bearbeiten',
      attachments: [attachment('gross.txt', 600 * 1024 * 1024)] }
    chat.queuedMessages['session-1'] = [entry]
    chat.current.messages = [{ id: 'sent', role: 'user', content: entry.content, attachments: [{ filename: 'gross.txt' }] }]
    api.delete.mockRejectedValueOnce({ response: { status: 409, data: { error: {
      code: 'session.runAlreadyActive', params: { messageId: 'sent' }
    } } } })
    api.post.mockResolvedValue({ data: { draft: { content: entry.content } } })
    await chat.restoreQueuedMessage(entry.id)
    expect(chat.activeAttachmentDraft.map(({ file }) => file.name)).toEqual(['gross.txt'])
  })

  it('keeps the server copy when restoring cannot clean up local storage', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.queuedMessages['session-1'] = [{ id: 'server-request', sessionId: 'session-1',
      content: 'Keep this', serverQueued: true, paused: false, attachments: [] }]
    const indexedDB = globalThis.indexedDB
    globalThis.indexedDB = undefined
    try {
      await chat.restoreQueuedMessage('server-request')
      expect([api.delete.mock.calls.length, chat.activeQueuedMessages[0].paused]).toEqual([0, false])
    } finally { globalThis.indexedDB = indexedDB }
  })

  it('clears the persisted outbox with the owner drafts', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    await chat.queueMessage('Vertraulich')

    await chat.clearAllDrafts()
    setActivePinia(createPinia())
    const restored = useChatStore()
    restored.setDraftOwner('user-1')
    await restored.outboxLoadPromise
    restored.cancelQueueDrain()
    activeSession(restored)

    expect(restored.activeQueuedMessages).toEqual([])
  })

  it('keeps queued messages in memory when persistent deletion fails', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    await chat.queueMessage('Nicht verlieren')
    const indexedDB = globalThis.indexedDB
    globalThis.indexedDB = undefined

    try {
      await expect(chat.clearQueuedMessages('session-1')).rejects.toThrow()
      expect(chat.activeQueuedMessages).toHaveLength(1)
    } finally {
      globalThis.indexedDB = indexedDB
    }
  })

  it.each([false, true])('keeps recovery on an accepted message when local cleanup fails (new session: %s)', async (newSession) => {
    const chat = useChatStore()
    if (!newSession) activeSession(chat)
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})
    vi.spyOn(useControlCenterStore(), 'attachCreatedSession').mockResolvedValue(null)
    const entry = await chat.queueMessage('Angenommen')
    api.post.mockResolvedValue({ data: {
      session: { id: 'session-1' }, runId: entry.id,
      message: { id: 'accepted', role: 'user', content: entry.content }
    } })
    const indexedDB = globalThis.indexedDB
    globalThis.indexedDB = undefined
    try {
      await chat.submitOutboxEntry(entry)
    } finally {
      globalThis.indexedDB = indexedDB
    }
    const displayed = withQueuedTranscriptMessages(chat.current.messages, chat.activeQueuedMessages, null)
    if (!newSession) await chat.refreshServerQueue()
    chat.running = false
    await chat.resumeQueuedSend()
    chat.cancelQueueDrain()
    await chat.drainOutbox()
    expect([displayed, chat.activeQueuedMessages, api.post.mock.calls.filter(([url]) => url !== '/sessions/defaults').length]).toEqual([
      newSession ? [{ id: 'accepted', role: 'user', content: entry.content, renderId: entry.id, queueRecoveryId: entry.id }]
        : [expect.objectContaining({ id: entry.id, content: entry.content, deliveryStatus: 'queued' })], [], 1
    ])
  })

  it('removes project outbox entries before deletion and restores them after failure', async () => {
    const chat = useChatStore()
    chat.current = { id: 'session-project', projectId: 'project-1', messages: [] }
    chat.selectedSessionId = 'session-project'
    chat.running = true
    const projectEntry = await chat.queueMessage('Projekt behalten')
    chat.current = { id: 'session-other', projectId: 'project-2', messages: [] }
    chat.selectedSessionId = 'session-other'
    const otherEntry = await chat.queueMessage('Andere Queue')
    vi.spyOn(chat, 'scheduleQueueDrain').mockImplementation(() => {})

    const snapshot = await chat.prepareProjectOutboxDeletion('project-1', ['session-project'])

    expect(chat.queuedMessages['session-project']).toBeUndefined()
    expect(chat.queuedMessages['session-other']).toHaveLength(1)
    expect((await loadChatOutbox('user-1')).map((entry) => entry.id)).toEqual([otherEntry.id])

    await chat.restoreProjectOutboxDeletion(snapshot)

    expect(chat.queuedMessages['session-project']).toHaveLength(1)
    expect((await loadChatOutbox('user-1')).map((entry) => entry.id).sort())
      .toEqual([projectEntry.id, otherEntry.id].sort())
  })

  it('aborts project outbox preparation without changing memory when persistence fails', async () => {
    const chat = useChatStore()
    chat.current = { id: 'session-project', projectId: 'project-1', messages: [] }
    chat.selectedSessionId = 'session-project'
    chat.running = true
    await chat.queueMessage('Nicht verlieren')
    vi.spyOn(chat, 'scheduleQueueDrain').mockImplementation(() => {})
    const indexedDB = globalThis.indexedDB
    globalThis.indexedDB = undefined

    try {
      await expect(chat.prepareProjectOutboxDeletion('project-1', ['session-project']))
        .rejects.toThrow()
      expect(chat.queuedMessages['session-project']).toHaveLength(1)
    } finally {
      globalThis.indexedDB = indexedDB
    }
  })

  it('aborts project deletion cleanup when the persisted outbox is unavailable', async () => {
    const chat = useChatStore()
    chat.outboxLoaded = false
    chat.outboxLoadFailed = true

    await expect(chat.prepareProjectOutboxDeletion('project-1'))
      .rejects.toThrow('Chat outbox is unavailable')
  })

  it('clears successful project queues locally and reschedules unrelated queues', async () => {
    const chat = useChatStore()
    chat.current = { id: 'session-project', projectId: 'project-1', messages: [] }
    chat.selectedSessionId = 'session-project'
    chat.running = true
    await chat.queueMessage('Projekt löschen')
    chat.current = { id: 'session-other', projectId: 'project-2', messages: [] }
    chat.selectedSessionId = 'session-other'
    const otherEntry = await chat.queueMessage('Andere Queue')
    const scheduleQueueDrain = vi.spyOn(chat, 'scheduleQueueDrain').mockImplementation(() => {})
    await chat.prepareProjectOutboxDeletion('project-1', ['session-project'])
    scheduleQueueDrain.mockClear()

    await chat.removeProjectScope('project-1', ['session-project'])

    expect(chat.queuedMessages['session-project']).toBeUndefined()
    expect(chat.queuedMessages['session-other']).toHaveLength(1)
    expect(scheduleQueueDrain).toHaveBeenCalledWith(0)
    expect((await loadChatOutbox('user-1')).map((entry) => entry.id)).toEqual([otherEntry.id])
  })

  it('restores the persistent queue when session deletion fails', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    await chat.queueMessage('Nachricht behalten')
    api.delete.mockRejectedValue(new Error('offline'))

    await expect(chat.deleteSession('session-1')).rejects.toThrow('offline')
    setActivePinia(createPinia())
    const restored = useChatStore()
    restored.setDraftOwner('user-1')
    await restored.outboxLoadPromise
    restored.cancelQueueDrain()
    activeSession(restored)

    expect(restored.activeQueuedMessages.map((entry) => entry.content)).toEqual(['Nachricht behalten'])
  })

  it('does not restore a failed deletion after the owner was cleared', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    await chat.queueMessage('Nicht wiederherstellen')
    api.delete.mockImplementation(async () => {
      await chat.clearAllDrafts()
      chat.setDraftOwner(null)
      throw new Error('session expired')
    })

    await expect(chat.deleteSession('session-1')).rejects.toThrow('session expired')
    chat.setDraftOwner('user-1')
    await chat.outboxLoadPromise
    chat.cancelQueueDrain()
    activeSession(chat)

    expect(chat.activeQueuedMessages).toEqual([])
  })

  it('does not resurrect an in-flight message deleted with its session', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    await chat.queueMessage('Verloren')
    chat.running = false
    api.post.mockImplementation(async () => {
      await chat.clearQueuedMessages('session-1')
      throw new Error('offline')
    })

    await chat.drainOutbox()

    expect(chat.queuedMessages['session-1']).toBeUndefined()
    expect(chat.queuedSendPausedSessions['session-1']).toBeUndefined()
  })

  it('sends a ready queue in another session in the background', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    const entry = await chat.queueMessage('Im Hintergrund')
    activeSession(chat, 'session-2')
    chat.running = false
    api.get.mockImplementation((url) => Promise.resolve({
      data: url === '/sessions/session-1' ? { activeRun: null } : { items: [], nextCursor: null }
    }))
    api.post.mockResolvedValue({ data: {} })

    await chat.drainOutbox()

    expect(api.post).toHaveBeenCalledWith('/sessions/session-1/messages', {
      content: 'Im Hintergrund',
      run_id: entry.id,
      enqueue: true,
    }, { suppressErrorToast: true })
    expect(chat.queuedMessages['session-1']).toBeUndefined()
  })

  it('watches a background send when its session is selected before server acceptance', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    await chat.queueMessage('Korrektur')
    activeSession(chat, 'session-2')
    chat.running = false
    api.get.mockImplementation(async (url) => ({ data: url === '/sessions/message-queue' ? { items: [] } : {
      id: 'session-1', messages: [{ id: 'correction', role: 'user', content: 'Korrektur' }], activeRun: { id: 'follow-up' }
    } }))
    let acceptSend
    api.post.mockImplementation(() => new Promise((resolve) => { acceptSend = resolve }))

    const sending = chat.drainOutbox()
    await vi.waitFor(() => expect(acceptSend).toBeTypeOf('function'))
    activeSession(chat)
    acceptSend({ data: {
      runId: 'follow-up', message: { id: 'correction', role: 'user', content: 'Korrektur' }
    } })
    await sending

    expect(chat.current.messages.map((message) => message.id)).toEqual(['correction'])
    expect(chat.eventSource.url).toBe('/api/sessions/session-1/runs/follow-up/events')
  })

  it('restores a background upload failure as paused', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    await chat.queueMessage('Im Hintergrund warten')
    activeSession(chat, 'session-2')
    chat.running = false
    api.get.mockRejectedValue(new Error('offline'))

    await chat.drainOutbox()
    setActivePinia(createPinia())
    const restored = useChatStore()
    restored.setDraftOwner('user-1')
    await restored.outboxLoadPromise
    restored.cancelQueueDrain()
    activeSession(restored)

    expect(restored.activeQueuedMessages).toHaveLength(1)
    expect(restored.queuedSendPaused).toBe(true)
  })

  it('keeps paused state isolated per session', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    await chat.queueMessage('A')
    chat.queuedSendPausedSessions['session-1'] = true
    chat.queuedSendPausedSessions['session-2'] = true

    await chat.resumeQueuedSend()
    chat.cancelQueueDrain()

    expect(chat.queuedSendPausedSessions['session-2']).toBe(true)
    expect(chat.queuedSendPaused).toBe(false)
  })

  it('counts queued attachments against the global draft budget', async () => {
    const chat = useChatStore()
    activeSession(chat)
    chat.running = true
    chat.attachmentDrafts.sessionMessages['session-1'] = [
      attachment('gross.bin', MAX_ATTACHMENT_DRAFT_BYTES)
    ]
    await chat.queueMessage('Groß')

    const result = chat.addAttachmentFiles([{ name: 'bild.png', type: 'image/png', size: 1024 }])

    expect(result.added).toBe(0)
    expect(result.error).toContain('1024 MiB')
  })
})
