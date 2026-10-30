import 'fake-indexeddb/auto'
import { createPinia, setActivePinia } from 'pinia'
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

import { useChatStore } from '../src/features/chat/store'
import { buildAttachmentFormData, MAX_ATTACHMENT_DRAFT_BYTES } from '../src/features/chat/attachments'
import { normalizedNewSessionDraft, sanitizedSessionOptions, sessionCreatePayload } from '../src/features/chat/state'
import { useControlCenterStore } from '../src/stores/control-center'
import { controlCenterIntegrationIds } from 'src/app/control-center/manifest'
import { useQuestionStore } from '../src/stores/questions'
import { useWorkspaceStore } from '../src/stores/workspace'
import {
  clearChatOutbox,
  loadChatOutbox,
  saveChatOutboxEntry
} from '../src/utils/chat-outbox'

const originalURL = globalThis.URL

function createStorage() {
  const values = new Map()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key)
  }
}

function sessionPage(items = [], nextCursor = null) {
  return { data: { items, nextCursor } }
}

function setSessions(chat, items, { loaded = true, nextCursor = null } = {}) {
  const list = chat.ensureSessionList()
  list.items = items
  list.loaded = loaded
  list.nextCursor = nextCursor
}

class EventSourceMock {
  static instances = []

  constructor(url) {
    this.url = url
    this.listeners = new Map()
    EventSourceMock.instances.push(this)
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener)
  }

  dispatchEvent(event) { this.listeners.get(event.type)?.(event) }

  emit(name, payload) {
    this.listeners.get(name)?.({ data: JSON.stringify(payload) })
  }

  close() {}
}

describe('chat drafts', () => {
  beforeEach(async () => {
    await clearChatOutbox('user-1')
    global.window = { localStorage: createStorage() }
    controlCenterIntegrationIds.add('calendar')
    setActivePinia(createPinia())
    const chat = useChatStore()
    chat.setDraftOwner('user-1')
    await chat.outboxLoadPromise
    vi.clearAllMocks()
    EventSourceMock.instances = []
  })

  afterEach(() => {
    controlCenterIntegrationIds.delete('calendar')
    vi.useRealTimers()
    delete global.window
    delete global.EventSource
    global.URL = originalURL
  })

  it('keeps separate drafts for new and existing sessions', () => {
    const chat = useChatStore()

    chat.setDraft('Neue Session Entwurf')
    chat.current = { id: 'session-1' }
    chat.setDraft('Bestehende Session Entwurf')

    expect(chat.activeDraft).toBe('Bestehende Session Entwurf')
    chat.current = null
    expect(chat.activeDraft).toBe('Neue Session Entwurf')
  })

  it('keeps activity time stable until the visible work changes', () => {
    const chat = useChatStore()
    const modelStartedAt = '2026-07-27T12:00:00.000Z'
    const repeatedEventAt = '2026-07-27T12:00:05.000Z'
    const firstToolStartedAt = '2026-07-27T12:00:10.000Z'
    const secondToolStartedAt = '2026-07-27T12:00:12.000Z'

    expect(chat.activity).toMatchObject({ providerAttempt: null, providerAttemptTotal: null })

    chat.setActivity('waiting_model', { modelRequest: 2, startedAt: modelStartedAt })
    chat.setActivity('waiting_model', {
      modelRequest: 2,
      providerAttempt: 2,
      providerAttemptTotal: 3,
      startedAt: repeatedEventAt
    })
    expect(chat.activity.startedAt).toBe(modelStartedAt)
    expect(chat.activity).toMatchObject({ providerAttempt: 2, providerAttemptTotal: 3 })

    chat.setActivity('processing_tool', {
      tool: 'read_file',
      toolDisplay: { group: 'filesystem', icon: 'folder', label: 'Projektdatei lesen' },
      toolCallId: 'call-1', index: 0, total: 2, startedAt: firstToolStartedAt
    })
    expect(chat.activity).toMatchObject({
      startedAt: firstToolStartedAt,
      tool: 'read_file',
      toolDisplay: { group: 'filesystem', icon: 'folder', label: 'Projektdatei lesen' },
      toolCallId: 'call-1',
      index: 0,
      total: 2
    })

    chat.setActivity('processing_tool', {
      tool: 'read_file', index: 1, total: 2, startedAt: secondToolStartedAt
    })
    expect(chat.activity.startedAt).toBe(secondToolStartedAt)
  })

  it('restores drafts from local storage', () => {
    const first = useChatStore()
    first.setDraft('Bleibt lokal erhalten')

    setActivePinia(createPinia())
    const restored = useChatStore()
    restored.setDraftOwner('user-1')

    expect(restored.activeDraft).toBe('Bleibt lokal erhalten')
  })

  it('migrates the persisted forced-search draft to the required policy', () => {
    window.localStorage.setItem('matoLoopChatDrafts', JSON.stringify({
      ownerUserId: 'user-1',
      newSession: { message: '', options: { forceWebSearch: true } },
      sessionMessages: {}
    }))

    const chat = useChatStore()
    chat.setDraftOwner(null)
    chat.setDraftOwner('user-1')

    expect(chat.activeContext.webSearchPolicy).toBe('required')
  })

  it('preserves restored session trust when loading draft defaults', async () => {
    window.localStorage.setItem('matoLoopChatDrafts', JSON.stringify({
      ownerUserId: 'user-1',
      newSession: { message: 'Alt', options: { trustLevel: 'private' } },
      sessionMessages: {}
    }))

    const chat = useChatStore()
    chat.setDraftOwner(null)
    chat.setDraftOwner('user-1')

    api.post.mockResolvedValueOnce({ data: { modelId: 'model-1' } })
    await chat.fetchDefaults()
    expect(chat.activeContext.trustLevel).toBe('private')
    chat.setDraft('Neu')
    const stored = JSON.parse(window.localStorage.getItem('matoLoopChatDrafts'))
    expect(stored.newSession.options.trustLevel).toBe('private')
  })

  it('clears removed workspace fields from persisted drafts', () => {
    window.localStorage.setItem('matoLoopChatDrafts', JSON.stringify({
      ownerUserId: 'user-1',
      newSession: {
        message: 'Alt',
        options: {
          preferredRootId: 'root-without-agent',
          workingDirectory: 'project',
          currentDirectory: '/home/user/project'
        }
      },
      sessionMessages: {}
    }))
    const chat = useChatStore()
    chat.setDraftOwner(null)
    chat.setDraftOwner('user-1')

    expect(chat.activeContext).toMatchObject({ preferredExecutionAgentId: null, currentDirectory: null })
    expect(chat.activeContext).not.toHaveProperty('preferredRootId')
    expect(chat.activeContext).not.toHaveProperty('workingDirectory')
  })

  it('sends the selected web-search policy when creating a session', async () => {
    api.post.mockResolvedValue({
      data: {
        session: { id: 'session-1', webSearchPolicy: 'required', messages: [] },
        runId: 'run-1'
      }
    })
    const chat = useChatStore()
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})
    await chat.updateActive({ web_search_policy: 'required' })

    await chat.startSession('Bitte recherchieren')

    expect(api.post).toHaveBeenCalledWith('/sessions', expect.objectContaining({
      web_search_policy: 'required'
    }))
    expect(chat.current.userRequestCount).toBe(1)
  })

  it('creates one session on the first app action and carries the draft into it', async () => {
    const chat = useChatStore()
    const center = useControlCenterStore()
    chat.setDraft('Browser prüfen')
    const attachment = { file: new Blob(['draft']), kind: 'text' }
    chat.attachmentDrafts.newSession.push(attachment)
    api.post.mockImplementation(async (_url, payload) => ({ data: {
      session: {
        id: 'session-browser', projectId: null, title: 'Neue Session',
        controlCenter: {
          tabs: payload.control_center.tabs.map((tab) => ({ appId: tab.app_id, pinned: false })),
          activeAppId: payload.control_center.active_app_id,
          open: payload.control_center.open
        }
      }
    } }))

    await center.setOpen(true)
    expect(api.post).not.toHaveBeenCalled()
    await center.openApp('browser')

    expect(api.post.mock.calls.filter(([url]) => url === '/sessions')).toHaveLength(1)
    expect(api.post.mock.calls[0][1]).toMatchObject({
      content: null,
      control_center: { active_app_id: 'browser', open: true }
    })
    expect(chat.current.id).toBe('session-browser')
    expect(center.sessionId).toBe('session-browser')
    expect(center.activeAppId).toBe('browser')
    expect(chat.activeDraft).toBe('Browser prüfen')
    expect(chat.activeAttachmentDraft).toEqual([attachment])
    expect(chat.drafts.newSession.message).toBe('')
    expect(chat.attachmentDrafts.newSession).toEqual([])
    expect((await chat.queueMessage(chat.activeDraft)).sessionId).toBe('session-browser')
  })

  it('keeps the new-session draft when app-triggered creation fails', async () => {
    const chat = useChatStore()
    const center = useControlCenterStore()
    chat.setDraft('Nicht verlieren')
    api.post.mockRejectedValueOnce(new Error('request failed'))

    await expect(center.openApp('browser')).rejects.toThrow('request failed')

    expect(chat.current).toBeNull()
    expect(center.sessionId).toBeNull()
    expect(chat.activeDraft).toBe('Nicht verlieren')
  })

  it('creates the session when the already visible Ablage tab is activated', async () => {
    const center = useControlCenterStore()
    api.post.mockImplementation(async (_url, payload) => ({ data: { session: {
      id: 'session-files', projectId: null, controlCenter: {
        tabs: payload.control_center.tabs.map((tab) => ({ appId: tab.app_id })),
        activeAppId: payload.control_center.active_app_id,
        open: true
      }
    } } }))

    await center.activate('files')

    expect(api.post.mock.calls.filter(([url]) => url === '/sessions')).toHaveLength(1)
    expect(useChatStore().current.id).toBe('session-files')
    expect(center.activeAppId).toBe('files')
  })

  it('creates only one empty session for overlapping app actions', async () => {
    const chat = useChatStore()
    const center = useControlCenterStore()
    let finishCreate
    api.post.mockReturnValue(new Promise((resolve) => { finishCreate = resolve }))
    api.patch.mockResolvedValue({ data: {
      controlCenter: {
        tabs: [{ appId: 'session' }, { appId: 'files' }, { appId: 'browser' }, { appId: 'mail' }],
        activeAppId: 'mail', open: true
      }
    } })

    const browser = center.openApp('browser')
    const mail = center.openApp('mail')
    await vi.waitFor(() => expect(finishCreate).toBeTypeOf('function'))
    finishCreate({ data: { session: { id: 'session-1', projectId: null, controlCenter: {
      tabs: [{ appId: 'session' }, { appId: 'files' }, { appId: 'browser' }],
      activeAppId: 'browser', open: true
    } } } })
    await Promise.all([browser, mail])

    expect(api.post.mock.calls.filter(([url]) => url === '/sessions')).toHaveLength(1)
    expect(api.patch).toHaveBeenCalledOnce()
    expect(chat.current.id).toBe('session-1')
    expect(center.activeAppId).toBe('mail')
  })

  it('persists and snapshots the selected agent profile for session creation', async () => {
    api.post.mockResolvedValue({
      data: {
        session: { id: 'session-agent-profile', agentProfileId: 'agent-1', messages: [] },
        runId: 'run-1'
      }
    })
    const chat = useChatStore()
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})

    await chat.updateActive({ agent_profile_id: 'agent-1' })
    const queued = await chat.queueMessage('Mit Profil')

    expect(queued.options.agentProfileId).toBe('agent-1')
    expect(JSON.parse(window.localStorage.getItem('matoLoopChatDrafts')).newSession.options)
      .toMatchObject({ agentProfileId: 'agent-1' })

    await chat.startSession('Mit Profil', [], { options: queued.options })

    expect(api.post).toHaveBeenCalledWith('/sessions', expect.objectContaining({
      agent_profile_id: 'agent-1'
    }))
    expect(chat.current.agentProfileId).toBe('agent-1')
  })

  it('lets the backend select the default profile while profiles are loading', async () => {
    api.post.mockResolvedValue({
      data: {
        session: { id: 'session-default-profile', agentProfileId: 'default', messages: [] },
        runId: 'run-1'
      }
    })
    const chat = useChatStore()
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})

    await chat.startSession('Ohne geladenes Profil')

    expect(api.post.mock.calls[0][1]).not.toHaveProperty('agent_profile_id')
  })

  it('persists and sends the session reasoning override', async () => {
    api.post.mockResolvedValue({
      data: {
        session: { id: 'session-1', reasoningEffort: 'ultra', messages: [] },
        runId: 'run-1'
      }
    })
    const chat = useChatStore()
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})

    await chat.updateActive({ reasoning_effort: 'ultra' })
    await chat.startSession('Kurz nachdenken')

    expect(api.post).toHaveBeenCalledWith('/sessions', expect.objectContaining({
      reasoning_effort: 'ultra'
    }))
  })

  it('does not reselect a created session after switching during tab reconciliation', async () => {
    let resolveTabs
    api.post.mockResolvedValue({
      data: {
        session: {
          id: 'created-session',
          messages: [],
          controlCenter: {
            tabs: [{ appId: 'files', pinned: false }],
            activeAppId: 'files'
          }
        },
        runId: 'run-1'
      }
    })
    api.patch.mockReturnValue(new Promise((resolve) => { resolveTabs = resolve }))
    const chat = useChatStore()
    const controlCenter = useControlCenterStore()
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})
    await controlCenter.save({
      tabs: [...controlCenter.tabs, { appId: 'calendar' }],
      activeAppId: 'calendar',
      open: true
    })

    const creating = chat.startSession('Neue Session')
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalled())
    api.get.mockReturnValue(new Promise(() => {}))
    void chat.loadSession('other-session')
    expect(controlCenter.sessionId).toBe('other-session')
    expect(controlCenter.ready).toBe(false)
    resolveTabs({
      data: {
        controlCenter: {
          tabs: [
            { appId: 'files', pinned: false },
            { appId: 'calendar', pinned: false }
          ],
          activeAppId: 'calendar'
        }
      }
    })

    expect(await creating).toEqual({ created: false, sessionId: 'created-session' })
    expect(chat.current).toBeNull()
    expect(chat.selectedSessionId).toBe('other-session')
  })

  it('clears only the submitted project draft after switching to another new chat', async () => {
    let resolveCreate
    api.post.mockReturnValue(new Promise((resolve) => { resolveCreate = resolve }))
    api.get.mockResolvedValue(sessionPage([]))
    const chat = useChatStore()
    chat.setDraft('Entwurf ohne Projekt')

    const creating = chat.startSession('Absenden')
    await vi.waitFor(() => expect(api.post).toHaveBeenCalled())
    await chat.setSessionScope('project-2')
    chat.setDraft('Projektentwurf behalten')
    resolveCreate({
      data: {
        session: { id: 'created-session', projectId: null, messages: [] },
        runId: 'run-1'
      }
    })

    expect(await creating).toEqual({ created: false, sessionId: 'created-session' })
    expect(chat.activeDraft).toBe('Projektentwurf behalten')
    expect(chat.drafts.newSessions.standalone.message).toBe('')
    expect(chat.running).toBe(false)
  })

  it('creates a session without computer file context', async () => {
    api.post.mockResolvedValue({
      data: { session: { id: 'session-1', messages: [] }, runId: 'run-1' }
    })
    const chat = useChatStore()
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})
    Object.assign(chat.activeContext, {
      preferredExecutionAgentId: null,
      currentDirectory: '/legacy/project'
    })

    await chat.startSession('Ohne Computer')

    expect(api.post).toHaveBeenCalledWith('/sessions', expect.objectContaining({
      preferred_execution_agent_id: null
    }))
    expect(api.post.mock.calls[0][1]).not.toHaveProperty('current_directory')
  })

  it('persists computer and absolute current directory and sends them when creating a session', async () => {
    api.post.mockResolvedValue({
      data: { session: { id: 'session-agent', messages: [] }, runId: 'run-agent' }
    })
    const chat = useChatStore()
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})

    await chat.updateActive({ preferred_execution_agent_id: 'agent-1' })
    await chat.updateActive({ current_directory: '/home/user/projekt' })
    expect(JSON.parse(window.localStorage.getItem('matoLoopChatDrafts')).newSession.options)
      .toMatchObject({
        preferredExecutionAgentId: 'agent-1',
        currentDirectory: '/home/user/projekt'
      })
    await chat.startSession('Lokal ausführen')

    expect(api.post).toHaveBeenCalledWith('/sessions', expect.objectContaining({
      preferred_execution_agent_id: 'agent-1',
      current_directory: '/home/user/projekt'
    }))
    const stored = JSON.parse(window.localStorage.getItem('matoLoopChatDrafts'))
    expect(stored.newSession.options.preferredExecutionAgentId).toBeNull()
  })

  it('clears the current directory when the computer changes', async () => {
    const chat = useChatStore()
    Object.assign(chat.activeContext, {
      preferredExecutionAgentId: 'agent-old',
      currentDirectory: '/home/user/projekt'
    })

    await chat.updateActive({ preferred_execution_agent_id: 'agent-new' })

    expect(chat.activeContext).toMatchObject({
      preferredExecutionAgentId: 'agent-new',
      currentDirectory: null
    })
  })

  it('keeps an explicitly supplied home when the computer changes', async () => {
    const chat = useChatStore()

    await chat.updateActive({
      preferred_execution_agent_id: 'agent-new',
      current_directory: '/workspace/home'
    })

    expect(chat.activeContext).toMatchObject({
      preferredExecutionAgentId: 'agent-new',
      currentDirectory: '/workspace/home'
    })
  })

  it('sends an explicitly supplied current folder for a persisted session', async () => {
    api.patch.mockResolvedValue({
      data: {
        id: 'session-1',
        currentDirectory: '/home/user'
      }
    })
    const chat = useChatStore()
    chat.current = {
      id: 'session-1',
      preferredExecutionAgentId: 'agent-1'
    }
    chat.selectedSessionId = 'session-1'

    await chat.updateActive({
      current_directory: '/home/user'
    })

    expect(api.patch).toHaveBeenCalledWith('/sessions/session-1', {
      current_directory: '/home/user'
    })
    expect(chat.current.currentDirectory).toBe('/home/user')
  })

  it('sends reset fields in one request when a persisted session changes executor', async () => {
    api.patch.mockResolvedValue({
      data: {
        id: 'session-1',
        preferredExecutionAgentId: 'agent-new',
        currentDirectory: '/home/new'
      }
    })
    const chat = useChatStore()
    chat.current = {
      id: 'session-1',
      preferredExecutionAgentId: 'agent-old',
      currentDirectory: '/home/old/projekt'
    }
    chat.selectedSessionId = 'session-1'

    await chat.updateActive({ preferred_execution_agent_id: 'agent-new' })

    expect(api.patch).toHaveBeenCalledWith('/sessions/session-1', {
      preferred_execution_agent_id: 'agent-new',
      current_directory: null
    })
    expect(chat.current.currentDirectory).toBe('/home/new')
  })

  it('drops draft directories without an explicit computer', async () => {
    const chat = useChatStore()

    await chat.updateActive({ current_directory: '/home/user' })

    expect(chat.activeContext.currentDirectory).toBeNull()
    expect(JSON.parse(window.localStorage.getItem('matoLoopChatDrafts')).newSession.options)
      .toMatchObject({ currentDirectory: null })

    api.patch.mockResolvedValue({ data: { id: 'session-1', currentDirectory: '/srv/project' } })
    chat.current = { id: 'session-1', preferredExecutionAgentId: 'agent-1', currentDirectory: '/srv' }
    chat.selectedSessionId = 'session-1'
    await chat.updateActive({ current_directory: '/should-not-be-sent', title: 'Test' })

    expect(api.patch).toHaveBeenCalledWith('/sessions/session-1', {
      current_directory: '/should-not-be-sent', title: 'Test'
    })
    expect(chat.current.currentDirectory).toBe('/srv/project')
  })

  it('applies and persists the backend default current directory', async () => {
    api.post.mockResolvedValue({
      data: {
        modelId: 'model-1',
        preferredExecutionAgentId: 'agent-1',
        currentDirectory: '/home/user'
      }
    })
    const chat = useChatStore()

    await chat.fetchDefaults()

    expect(chat.activeContext.preferredExecutionAgentId).toBe('agent-1')
    expect(chat.activeContext.currentDirectory).toBe('/home/user')
    expect(JSON.parse(window.localStorage.getItem('matoLoopChatDrafts')).newSession.options)
      .toMatchObject({ currentDirectory: '/home/user' })
  })

  it('loads execution defaults for the selected project', async () => {
    api.get.mockResolvedValueOnce(sessionPage())
    api.post.mockResolvedValueOnce({
      data: {
        modelId: 'model-1',
        sessionTemplateId: 'coding',
        preferredExecutionAgentId: 'agent-1',
        currentDirectory: '/srv/project'
      }
    })
    const chat = useChatStore()

    await chat.setSessionScope('project-1')
    await chat.fetchDefaults('project-1')

    expect(api.post).toHaveBeenCalledWith('/sessions/defaults', {
      project_id: 'project-1', session_template_id: null
    })
    expect(chat.drafts.newSessions['project-1'].options).toMatchObject({
      sessionTemplateId: 'coding',
      preferredExecutionAgentId: 'agent-1',
      currentDirectory: '/srv/project'
    })
  })

  it('preserves worktree selection in older drafts and queued requests without the support flag', () => {
    const options = { sessionProfile: { templateId: 'coding' }, preferredExecutionAgentId: 'agent', currentDirectory: '/repo', workspaceMode: 'worktree' }

    expect([
      normalizedNewSessionDraft({ options }).options.workspaceMode,
      sessionCreatePayload(sanitizedSessionOptions(options), 'project', {}).workspace_mode
    ]).toEqual(['worktree', 'worktree'])
  })

  it.each([
    [false, 'agent', '/repo', 'local'],
    [true, 'agent', '/repo', 'worktree'],
    [true, 'other', '/repo', 'local'],
    [true, 'agent', '/other', 'local']
  ])('applies the project workspace mode only for enabled templates and matching workspaces (%s, %s, %s)', async (enabled, agent, directory, mode) => {
    const chat = useChatStore()
    Object.assign(chat.drafts.newSession.options, { preferredExecutionAgentId: agent, currentDirectory: directory, workspaceMode: 'worktree' })
    api.post.mockResolvedValue({ data: { sessionTemplateId: 'template', sessionProfile: { worktreesEnabled: enabled }, workspaceMode: 'worktree', preferredExecutionAgentId: 'agent', currentDirectory: '/repo' } })

    await chat.updateActive({ session_template_id: 'template' })

    expect(chat.activeContext.workspaceMode).toBe(mode)
  })

  it('keeps the latest template choice when previews overlap with another project reset', async () => {
    const responses = []
    api.post.mockImplementation(() => new Promise((resolve) => responses.push(resolve)))
    api.get.mockResolvedValue(sessionPage())
    const chat = useChatStore()
    await chat.setSessionScope('project-b')
    const first = chat.updateActive({ session_template_id: 'first' })
    const latest = chat.updateActive({ session_template_id: 'latest' })
    chat.resetNewDraft('project-a')
    responses[2]({ data: { sessionTemplateId: 'project-a-default' } })
    responses[0]({ data: { sessionTemplateId: 'first' } })
    await first
    responses[1]({ data: { sessionTemplateId: 'latest' } })
    await latest
    expect(chat.activeContext.sessionTemplateId).toBe('latest')
  })

  it.each([false, true])('waits for project defaults before creation (empty=%s)', async (empty) => {
    const chat = useChatStore()
    let resolveDefaults
    api.get.mockResolvedValue(sessionPage())
    api.post.mockResolvedValue({ data: {} })
      .mockImplementationOnce(() => new Promise(resolve => { resolveDefaults = resolve }))
      .mockResolvedValueOnce({ data: {
        session: { id: 'created', projectId: 'project-1' },
        message: { id: 'message', role: 'user', content: 'Work' }, runId: 'run'
      } })
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})
    await chat.setSessionScope('project-1')
    const loading = chat.fetchDefaults()
    const creating = empty ? chat.createEmptySession(useControlCenterStore()) : chat.send('Work')
    const earlyCreations = api.post.mock.calls.filter(([url]) => url === '/sessions').length
    resolveDefaults({ data: { sessionTemplateId: 'coding', preferredExecutionAgentId: 'computer', currentDirectory: '/repo', memoryMode: 'disabled' } })
    await Promise.all([loading, creating])
    expect([earlyCreations, api.post.mock.calls.find(([url]) => url === '/sessions')?.[1]]).toEqual([
      0, expect.objectContaining({ session_template_id: 'coding', preferred_execution_agent_id: 'computer', current_directory: '/repo', memory_mode: 'disabled' })
    ])
  })

  it('waits for saved session settings before sending', async () => {
    let resolvePatch
    api.patch.mockReturnValue(new Promise((resolve) => { resolvePatch = resolve }))
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [], currentDirectory: null }
    chat.selectedSessionId = 'session-1'
    api.post.mockResolvedValue({ data: { session: { ...chat.current, currentDirectory: '/projekt' }, message: { id: 'message', role: 'user' }, runId: 'run' } })
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})

    const updating = chat.updateActive({ current_directory: '/projekt' })

    expect(chat.sessionSettingsPending).toBe(1)
    const sending = chat.send('Nicht mit altem Kontext senden')
    expect(chat.outboxSaving).toBe(true)
    expect(api.post).not.toHaveBeenCalled()

    resolvePatch({ data: { id: 'session-1', currentDirectory: '/projekt' } })
    await Promise.all([updating, sending])
    expect(chat.sessionSettingsPending).toBe(0)
    expect(api.post.mock.calls[0]?.[0]).toBe('/sessions/session-1/messages')
  })

  it('applies queued model and reasoning edits to the latest saved profile', async () => {
    const chat = useChatStore()
    chat.current = { id: 'session-1', sessionProfile: { agentModels: { agent: { modelId: 'old' } } } }
    chat.selectedSessionId = 'session-1'
    let resolveModel
    api.patch.mockImplementationOnce(() => new Promise(resolve => { resolveModel = resolve }))
      .mockImplementation(async (_url, patch) => ({ data: { id: 'session-1', sessionProfile: patch.session_profile } }))
    const model = chat.updateActive({ session_profile: profile => ({ ...profile, agentModels: { agent: { modelId: 'new' } } }) })
    const reasoning = chat.updateActive({ session_profile: profile => ({ ...profile, agentModels: {
      ...profile.agentModels, agent: { ...profile.agentModels.agent, reasoningEffort: 'high' }
    } }) })
    await vi.waitFor(() => expect(resolveModel).toBeTypeOf('function'))
    resolveModel({ data: { id: 'session-1', sessionProfile: { agentModels: { agent: { modelId: 'new' } } } } })
    await Promise.all([model, reasoning])
    expect(chat.current.sessionProfile.agentModels.agent).toEqual({ modelId: 'new', reasoningEffort: 'high' })
  })

  it('keeps attachments out of local storage and uses the general multipart route', async () => {
    api.post.mockResolvedValue({
      data: {
        session: { id: 'session-audio', messages: [] },
        message: {
          id: 'message-audio',
          role: 'user',
          content: 'Bitte transkribieren',
          attachments: [{ id: 'attachment-audio', kind: 'audio', mimeType: 'audio/wav' }]
        },
        runId: 'run-audio'
      }
    })
    const audio = new Blob(['RIFF----WAVE'], { type: 'audio/wav' })
    Object.defineProperty(audio, 'name', { value: 'aufnahme.wav' })
    const notes = new Blob(['Notizen'], { type: 'text/plain' })
    Object.defineProperty(notes, 'name', { value: 'notizen.txt' })
    const chat = useChatStore()
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})

    expect(chat.addAttachmentFiles([audio, notes])).toMatchObject({ added: 2, error: null })
    chat.setDraft('Bitte transkribieren')
    const persisted = window.localStorage.getItem('matoLoopChatDrafts')
    expect(persisted).not.toContain('aufnahme.wav')

    await expect(chat.send('Bitte transkribieren')).resolves.toMatchObject({ created: true })

    expect(api.post).toHaveBeenCalledWith(
      '/sessions/with-attachments',
      expect.any(FormData),
      { suppressErrorToast: true }
    )
    const formData = api.post.mock.calls[0][1]
    expect(JSON.parse(formData.get('payload'))).toMatchObject({ content: 'Bitte transkribieren' })
    expect(formData.getAll('attachments')).toHaveLength(2)
    expect(formData.getAll('attachments').map((file) => file.name))
      .toEqual(['aufnahme.wav', 'notizen.txt'])
    expect(chat.attachmentDrafts.newSession).toEqual([])
    expect(chat.current.messages[0].attachments).toHaveLength(1)
  })

  it('sends attachments without message text', async () => {
    api.post.mockResolvedValue({
      data: {
        message: {
          id: 'message-document',
          role: 'user',
          content: '',
          attachments: [{ id: 'attachment-document', kind: 'document', mimeType: 'application/pdf' }]
        },
        runId: 'run-document'
      }
    })
    const document = new Blob(['PDF'], { type: 'application/pdf' })
    Object.defineProperty(document, 'name', { value: 'bericht.pdf' })
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [] }
    chat.selectedSessionId = 'session-1'
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})
    chat.addAttachmentFiles([document])

    await chat.send('')

    expect(api.post).toHaveBeenCalledWith(
      '/sessions/session-1/messages/with-attachments',
      expect.any(FormData),
      { suppressErrorToast: true }
    )
    const formData = api.post.mock.calls[0][1]
    expect(JSON.parse(formData.get('payload'))).toMatchObject({
      content: '',
      run_id: expect.any(String)
    })
    expect(formData.getAll('attachments')).toHaveLength(1)
    expect(chat.current.messages).toEqual([expect.objectContaining({ content: '' })])
    expect(chat.activeAttachmentDraft).toEqual([])
  })

  it('uses the message multipart route and retains a generic file after an API failure', async () => {
    api.post.mockRejectedValue(new Error('offline'))
    const document = new Blob(['PDF'], { type: 'application/pdf' })
    Object.defineProperty(document, 'name', { value: 'fehler.pdf' })
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [] }
    chat.addAttachmentFiles([document])

    await expect(chat.send('Bitte prüfen')).resolves.toBeNull()

    expect(api.post).toHaveBeenCalledWith(
      '/sessions/session-1/messages/with-attachments',
      expect.any(FormData),
      { suppressErrorToast: true }
    )
    expect(chat.activeAttachmentDraft).toHaveLength(0)
    expect(chat.activeQueuedMessages).toHaveLength(1)
    expect(chat.queuedSendPaused).toBe(true)
    expect(chat.current.messages).toEqual([])
  })

  it('accepts generic files and rejects too many, empty and oversized attachments', () => {
    const chat = useChatStore()
    const wav = () => {
      const file = new Blob(['wav'], { type: 'audio/wav' })
      Object.defineProperty(file, 'name', { value: 'audio.wav' })
      return file
    }

    expect(chat.addAttachmentFiles(Array.from({ length: 9 }, wav)).error).toContain('höchstens 8')
    expect(chat.addAttachmentFiles([{ name: 'leer.txt', type: 'text/plain', size: 0 }]).error)
      .toContain('leer')
    expect(chat.addAttachmentFiles([{
      name: 'gross.pdf',
      type: 'application/pdf',
      size: 1024 * 1024 * 1024 + 1
    }]).error).toContain('1024 MiB')
    expect(chat.addAttachmentFiles([{ name: 'notizen.txt', type: 'text/plain', size: 10 }]))
      .toMatchObject({ added: 1, error: null })
    expect(chat.activeAttachmentDraft[0]).toMatchObject({ kind: 'file', mimeType: 'text/plain' })

    expect(chat.addAttachmentFiles([{ name: 'clip.mp4', type: 'video/mp4', size: 10 }]))
      .toMatchObject({ added: 1, error: null })
    expect(chat.activeAttachmentDraft[1]).toMatchObject({ kind: 'video', mimeType: 'video/mp4' })
  })

  it('limits attachment drafts across sessions and previews only admitted audio files', () => {
    const createObjectURL = vi.fn()
    global.URL = { createObjectURL, revokeObjectURL: vi.fn() }
    const chat = useChatStore()
    const fullFile = (name) => ({
      name: `${name}.wav`,
      type: 'audio/wav',
      size: MAX_ATTACHMENT_DRAFT_BYTES / 4
    })

    expect(chat.addAttachmentFiles([fullFile('neu')])).toMatchObject({ added: 1, error: null })
    for (const id of ['session-1', 'session-2', 'session-3']) {
      chat.current = { id }
      expect(chat.addAttachmentFiles([fullFile(id)])).toMatchObject({ added: 1, error: null })
    }
    chat.current = { id: 'session-4' }
    expect(chat.addAttachmentFiles([{ name: 'zwei.wav', type: 'audio/wav', size: 1 }]).error)
      .toContain('1024 MiB')
    expect(createObjectURL).toHaveBeenCalledTimes(4)
  })

  it('creates image previews and revokes them when removing or clearing drafts', () => {
    const createObjectURL = vi.fn(() => 'blob:preview')
    const revokeObjectURL = vi.fn()
    global.URL = { createObjectURL, revokeObjectURL }
    const chat = useChatStore()

    chat.addAttachmentFiles([{ name: 'bild.png', type: 'image/png', size: 10 }])

    expect(chat.activeAttachmentDraft[0]).toMatchObject({
      kind: 'image',
      objectUrl: 'blob:preview'
    })
    chat.removeAttachmentFile(chat.activeAttachmentDraft[0].id)
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:preview')
  })

  it('clears every attachment draft and revokes preview URLs', () => {
    const revokeObjectURL = vi.fn()
    global.URL = { revokeObjectURL }
    const chat = useChatStore()
    chat.attachmentDrafts = {
      newSession: [{ id: 'new', file: { size: 1 }, objectUrl: 'blob:new' }],
      sessionMessages: {
        'session-1': [{ id: 'old', file: { size: 1 }, objectUrl: 'blob:old' }]
      }
    }
    chat.setAudioDictation({ file: { size: 2 }, status: 'failed' }, 'session-1')

    chat.clearAllAttachmentDrafts()

    expect(chat.attachmentDrafts).toEqual({
      newSession: [],
      newSessions: { standalone: [] },
      sessionMessages: {}
    })
    expect(chat.audioDictations).toEqual({
      newSession: null,
      newSessions: { standalone: null },
      sessionMessages: {}
    })
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:new')
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:old')
  })

  it('clears all known session and draft state for a deleted project scope', async () => {
    const revokeObjectURL = vi.fn()
    global.URL = { revokeObjectURL }
    const chat = useChatStore()
    vi.spyOn(chat, 'scheduleQueueDrain').mockImplementation(() => {})
    chat.sessionsProjectId = 'project-1'
    chat.sessionLists = {
      standalone: { ...chat.ensureSessionList(null), items: [] },
      'project-1': {
        ...chat.ensureSessionList('project-1'),
        items: [{ id: 'session-list', projectId: 'project-1' }]
      },
      'project-2': {
        ...chat.ensureSessionList('project-2'),
        items: [
          { id: 'session-list', projectId: 'project-1' },
          { id: 'session-keep', projectId: 'project-2' }
        ]
      }
    }
    chat.sessionCatalog = [
      { id: 'session-catalog', projectId: 'project-1' },
      { id: 'session-keep', projectId: 'project-2' }
    ]
    chat.current = { id: 'session-current', projectId: 'project-1' }
    chat.selectedSessionId = 'session-current'
    chat.runSessionId = 'session-current'
    chat.runId = 'run-1'
    chat.running = true
    chat.drafts.newSessions['project-1'] = { message: 'Neu', options: {} }
    chat.drafts.newSession = chat.drafts.newSessions['project-1']
    chat.drafts.sessionMessages = {
      'session-list': 'Liste',
      'session-catalog': 'Katalog',
      'session-current': 'Aktiv',
      'session-queued': 'Queue',
      'session-unloaded': 'Nicht geladen',
      'session-keep': 'Behalten'
    }
    chat.attachmentDrafts.newSessions['project-1'] = [{ objectUrl: 'blob:new-project' }]
    chat.attachmentDrafts.newSession = chat.attachmentDrafts.newSessions['project-1']
    chat.attachmentDrafts.sessionMessages = {
      'session-list': [{ objectUrl: 'blob:list' }],
      'session-catalog': [{ objectUrl: 'blob:catalog' }],
      'session-current': [{ objectUrl: 'blob:current' }],
      'session-queued': [{ objectUrl: 'blob:queued' }],
      'session-unloaded': [{ objectUrl: 'blob:unloaded' }],
      'session-keep': [{ objectUrl: 'blob:keep' }]
    }
    chat.audioDictations.newSessions['project-1'] = { status: 'ready' }
    chat.audioDictations.newSession = chat.audioDictations.newSessions['project-1']
    chat.audioDictations.sessionMessages = {
      'session-list': { status: 'ready' },
      'session-catalog': { status: 'ready' },
      'session-current': { status: 'ready' },
      'session-queued': { status: 'ready' },
      'session-unloaded': { status: 'ready' },
      'session-keep': { status: 'ready' }
    }
    chat.queuedMessages = {
      'session-queued': [{ id: 'queue-1', sessionId: 'session-queued', projectId: 'project-1', attachments: [] }],
      'new:project-1': [{ id: 'queue-2', sessionId: null, projectId: 'project-1', attachments: [] }],
      'session-keep': [{ id: 'queue-3', sessionId: 'session-keep', projectId: 'project-2', attachments: [] }]
    }

    await chat.removeProjectScope('project-1', [
      'session-list',
      'session-catalog',
      'session-current',
      'session-queued',
      'session-unloaded'
    ])

    expect(chat.sessionsProjectId).toBeNull()
    expect(chat.sessionLists['project-1']).toBeUndefined()
    expect(chat.sessionLists['project-2'].items).toEqual([{ id: 'session-keep', projectId: 'project-2' }])
    expect(chat.sessionCatalog).toEqual([{ id: 'session-keep', projectId: 'project-2' }])
    expect(chat.current).toBeNull()
    expect(chat.selectedSessionId).toBeNull()
    expect(chat.runSessionId).toBeNull()
    expect(chat.runId).toBeNull()
    expect(chat.drafts.sessionMessages).toEqual({ 'session-keep': 'Behalten' })
    expect(chat.attachmentDrafts.sessionMessages).toEqual({
      'session-keep': [{ objectUrl: 'blob:keep' }]
    })
    expect(chat.audioDictations.sessionMessages).toEqual({
      'session-keep': { status: 'ready' }
    })
    expect(chat.queuedMessages).toEqual({
      'session-keep': [{ id: 'queue-3', sessionId: 'session-keep', projectId: 'project-2', attachments: [] }]
    })
    expect(chat.drafts.newSessions['project-1']).toBeUndefined()
    expect(chat.attachmentDrafts.newSessions['project-1']).toBeUndefined()
    expect(chat.audioDictations.newSessions['project-1']).toBeUndefined()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:new-project')
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:list')
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:catalog')
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:current')
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:queued')
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:unloaded')
    expect(revokeObjectURL).not.toHaveBeenCalledWith('blob:keep')
  })

  it('clears persisted text and attachment drafts together', () => {
    const chat = useChatStore()
    chat.setDraft('Vertraulicher Entwurf')
    chat.attachmentDrafts.newSession.push({ id: 'file', file: { size: 1 } })

    chat.clearAllDrafts()

    expect(chat.activeDraft).toBe('')
    expect(chat.attachmentDrafts).toEqual({
      newSession: [],
      newSessions: { standalone: [] },
      sessionMessages: {}
    })
    expect(window.localStorage.getItem('matoLoopChatDrafts')).toBeNull()
  })

  it('does not restore drafts owned by another user', () => {
    window.localStorage.setItem('matoLoopChatDrafts', JSON.stringify({
      ownerUserId: 'user-2',
      newSession: { message: 'Fremder Entwurf', options: { modelId: 'private-model' } },
      sessionMessages: { private: 'Fremde Session' }
    }))
    const chat = useChatStore()
    chat.setDraftOwner(null)
    chat.setDraftOwner('user-1')

    expect(chat.activeDraft).toBe('')
    expect(chat.activeContext.modelId).toBeNull()
    expect(chat.drafts.sessionMessages).toEqual({})
    expect(JSON.parse(window.localStorage.getItem('matoLoopChatDrafts')).ownerUserId).toBe('user-1')
  })

  it('keeps the effective memory mode in sync for a new session draft', async () => {
    api.post.mockResolvedValue({ data: { effectiveMemoryMode: 'read_only' } })
    const chat = useChatStore()

    await chat.updateActive({ memory_mode: 'read_only' })

    expect(chat.activeContext.memoryMode).toBe('read_only')
    expect(chat.activeContext.effectiveMemoryMode).toBe('read_only')
  })

  it('keeps instant memory active without model tool support', async () => {
    api.post.mockResolvedValue({ data: { effectiveMemoryMode: 'read_only', memoryToolsAvailable: false } })
    const chat = useChatStore()
    chat.activeContext.memoryUnavailableReason = 'memory.modelToolsUnavailable'
    chat.activeContext.memoryToolsAvailable = false

    await chat.updateActive({ memory_mode: 'read_only' })

    expect(chat.activeContext.effectiveMemoryMode).toBe('read_only')
  })

  it('refreshes effective memory when the draft model changes', async () => {
    api.post.mockResolvedValue({
      data: {
        modelId: 'private-model',
        effectiveMemoryMode: 'disabled',
        memoryToolsAvailable: false,
        memoryUnavailableReason: 'memory.modelDenied'
      }
    })
    const chat = useChatStore()

    await chat.updateActive({ model_id: 'private-model' })

    expect(api.post).toHaveBeenCalledWith('/sessions/defaults', expect.objectContaining({ model_id: 'private-model', memory_mode: 'full' }))
    expect(chat.activeContext).toMatchObject({
      modelId: 'private-model',
      effectiveMemoryMode: 'disabled',
      memoryToolsAvailable: false,
      memoryUnavailableReason: 'memory.modelDenied'
    })
  })

  it('preserves the draft memory mode while refreshing defaults', async () => {
    const chat = useChatStore()
    chat.activeContext.modelId = 'saved-model'
    chat.activeContext.memoryMode = 'read_only'
    api.post
      .mockResolvedValueOnce({
        data: {
          effectiveMemoryMode: 'read_only',
          memoryToolsAvailable: true,
          memoryUnavailableReason: null
        }
      })

    await chat.fetchDefaults()

    expect(api.post).toHaveBeenCalledExactlyOnceWith('/sessions/defaults', expect.objectContaining({ model_id: 'saved-model', memory_mode: 'read_only' }))
    expect(chat.activeContext).toMatchObject({
      modelId: 'saved-model',
      memoryMode: 'read_only',
      effectiveMemoryMode: 'read_only'
    })
  })

  it('applies effective defaults when a persisted model is removed', async () => {
    api.post.mockResolvedValue({ data: { effectiveMemoryMode: 'read_only', memoryToolsAvailable: true, memoryUnavailableReason: null } })
    const chat = useChatStore()
    chat.sessionDefaults = {
      modelId: 'default-model',
      effectiveMemoryMode: 'full',
      memoryToolsAvailable: true,
      memoryUnavailableReason: null
    }
    Object.assign(chat.activeContext, {
      modelId: 'removed-model',
      memoryMode: 'read_only',
      effectiveMemoryMode: 'disabled',
      memoryToolsAvailable: false,
      memoryUnavailableReason: 'session.modelUnavailable'
    })

    await chat.reconcileNewSession(['default-model'], true)

    expect(chat.activeContext).toMatchObject({
      modelId: 'default-model',
      memoryMode: 'read_only',
      effectiveMemoryMode: 'read_only',
      memoryToolsAvailable: true,
      memoryUnavailableReason: null
    })
  })

  it('ignores a stale session response after navigation', async () => {
    const requests = {}
    api.get.mockImplementation((url, config) => new Promise((resolve) => {
      requests[url] = { resolve, config }
    }))
    const chat = useChatStore()
    chat.current = { id: 'previous-session', messages: [{ id: 'old-message' }] }

    const firstLoad = chat.loadSession('session-1')
    expect(chat.current).toBeNull()
    const secondLoad = chat.loadSession('session-2')
    expect(requests['/sessions/session-1'].config.signal.aborted).toBe(true)
    requests['/sessions/session-2'].resolve({
      data: { id: 'session-2', messages: [], activeRun: null }
    })
    await secondLoad
    requests['/sessions/session-1'].resolve({
      data: { id: 'session-1', messages: [], activeRun: null }
    })
    await firstLoad

    expect(chat.current.id).toBe('session-2')
    expect(chat.loading).toBe(false)
    expect(chat.loadError).toBeNull()
  })

  it('keeps an update received while the selected session is initially loading', async () => {
    let finishLoad
    api.get.mockReturnValueOnce(new Promise((resolve) => { finishLoad = resolve }))
      .mockResolvedValue({ data: { id: 'session-1', messages: [{ id: 'new', role: 'user' }] } })
    const chat = useChatStore()
    const loading = chat.loadSession('session-1')
    const refreshing = chat.refreshSession('session-1')
    finishLoad({ data: { id: 'session-1', messages: [] } })
    await Promise.all([loading, refreshing])
    expect(chat.current.messages.map((message) => message.id)).toEqual(['new'])
  })

  it.each([true, false])('acknowledges the session with or without an unread assistant message (%s)', async (hasMessage) => {
    api.get.mockResolvedValue({
      data: {
        id: 'session-1',
        hasUnreadMessages: hasMessage,
        activeRun: null,
        messages: hasMessage ? [
          { id: 'user-message', role: 'user' },
          { id: 'assistant-message', role: 'assistant' }
        ] : []
      }
    })
    api.post.mockResolvedValue({ data: { read: true } })
    const chat = useChatStore()
    setSessions(chat, [{ id: 'session-1', hasUnreadMessages: hasMessage }])

    await chat.loadSession('session-1')
    await chat.markSessionRead(chat.current)
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith(
      '/sessions/session-1/read',
      hasMessage ? { throughMessageId: 'assistant-message' } : {},
      { suppressErrorToast: true }
    ))

    expect(chat.current.hasUnreadMessages).toBe(false)
    expect(chat.sessions[0].hasUnreadMessages).toBe(false)
  })

  it('prepends older message pages without duplicates', async () => {
    api.get.mockResolvedValue({
      data: {
        items: [
          { id: 'user-1', role: 'user' },
          { id: 'assistant-1', role: 'assistant' },
          { id: 'user-2', role: 'user' }
        ],
        contextSummaries: [
          { id: 'summary-1' },
          { id: 'summary-2' }
        ],
        nextCursor: null
      }
    })
    const chat = useChatStore()
    chat.current = {
      id: 'session-1',
      nextMessageCursor: 'user-2',
      messages: [{ id: 'user-2', role: 'user' }, { id: 'assistant-2', role: 'assistant' }],
      contextSummaries: [{ id: 'summary-2' }]
    }

    const added = await chat.loadOlderMessages()

    expect(api.get).toHaveBeenCalledWith('/sessions/session-1/messages', {
      params: { cursor: 'user-2', pageSize: 20 }
    })
    expect(added.map((message) => message.id)).toEqual(['user-1', 'assistant-1'])
    expect(chat.current.messages.map((message) => message.id)).toEqual([
      'user-1', 'assistant-1', 'user-2', 'assistant-2'
    ])
    expect(chat.current.contextSummaries.map((summary) => summary.id)).toEqual([
      'summary-1', 'summary-2'
    ])
    expect(chat.current.nextMessageCursor).toBeNull()
  })

  it('shares an in-flight older-message request', async () => {
    let resolvePage
    api.get.mockReturnValue(new Promise((resolve) => { resolvePage = resolve }))
    const chat = useChatStore()
    chat.current = { id: 'session-1', nextMessageCursor: 'cursor-1', messages: [] }

    const first = chat.loadOlderMessages()
    const second = chat.loadOlderMessages()
    resolvePage({ data: { items: [{ id: 'user-1' }], nextCursor: null } })
    await Promise.all([first, second])

    expect(api.get).toHaveBeenCalledOnce()
    expect(chat.current.messages).toEqual([{ id: 'user-1' }])
  })

  it('merges a latest-page refresh into already loaded history', async () => {
    api.get.mockResolvedValue({
      data: {
        id: 'session-1',
        messages: [
          { id: 'user-2', content: 'Aktualisiert' },
          { id: 'assistant-2', content: 'Neu' }
        ],
        contextSummaries: [
          { id: 'summary-2', content: 'Aktualisiert' },
          { id: 'summary-3', content: 'Neu' }
        ],
        nextMessageCursor: 'server-cursor'
      }
    })
    const chat = useChatStore()
    chat.selectedSessionId = 'session-1'
    chat.current = {
      id: 'session-1',
      messages: [{ id: 'user-1' }, { id: 'user-2', content: 'Alt' }],
      contextSummaries: [
        { id: 'summary-1', content: 'Alt' },
        { id: 'summary-2', content: 'Alt' }
      ],
      nextMessageCursor: 'older-cursor'
    }

    await chat.refreshSession('session-1')

    expect(chat.current.messages).toEqual([
      { id: 'user-1' },
      { id: 'user-2', content: 'Aktualisiert' },
      { id: 'assistant-2', content: 'Neu' }
    ])
    expect(chat.current.contextSummaries).toEqual([
      { id: 'summary-1', content: 'Alt' },
      { id: 'summary-2', content: 'Aktualisiert' },
      { id: 'summary-3', content: 'Neu' }
    ])
    expect(chat.current.nextMessageCursor).toBe('older-cursor')
  })

  it('discovers an externally started run while refreshing the open session', async () => {
    global.EventSource = EventSourceMock
    api.get.mockResolvedValue({
      data: {
        id: 'session-1',
        messages: [{ id: 'telegram-1', role: 'user', content: 'Von Telegram' }],
        contextSummaries: [],
        activeRun: { id: 'run-1', status: 'running' }
      }
    })
    const chat = useChatStore()
    chat.selectedSessionId = 'session-1'
    chat.current = { id: 'session-1', messages: [], contextSummaries: [] }

    await chat.refreshSession('session-1', { suppressErrorToast: true })

    expect(chat.current.messages).toEqual([
      { id: 'telegram-1', role: 'user', content: 'Von Telegram' }
    ])
    expect(api.get).toHaveBeenCalledWith('/sessions/session-1', expect.objectContaining({
      suppressErrorToast: true
    }))
    expect(chat.eventSource.url).toBe('/api/sessions/session-1/runs/run-1/events')
  })

  it('batches queued session refreshes without losing changes after an in-flight read', async () => {
    let finishFirst
    api.get.mockReturnValueOnce(new Promise((resolve) => { finishFirst = resolve }))
      .mockResolvedValue({ data: { id: 'session-1', messages: [{ id: 'new' }] } })
    const chat = useChatStore()
    chat.selectedSessionId = 'session-1'
    chat.current = { id: 'session-1', messages: [] }
    const first = chat.refreshSession('session-1')
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledOnce())
    const second = chat.refreshSession('session-1')
    const third = chat.refreshSession('session-1')
    finishFirst({ data: { id: 'session-1', messages: [] } })
    await Promise.all([first, second, third])
    expect([api.get.mock.calls.length, chat.current.messages.map((message) => message.id)]).toEqual([2, ['new']])
  })

  it('streams and promotes a canonical context summary', async () => {
    global.EventSource = EventSourceMock
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [], contextSummaries: [] }
    chat.selectedSessionId = 'session-1'
    const refresh = vi.spyOn(chat, 'refreshSession').mockResolvedValue(chat.current)

    chat.watchRun('session-1', 'run-1', { status: 'running' })
    const source = EventSourceMock.instances[0]
    source.emit('context.summary.updated', {
      summary: { content: 'Goal\nWeiterarbeiten', isFallback: false, status: 'streaming' }
    })

    expect(chat.streamContextSummary).toEqual({
      content: 'Goal\nWeiterarbeiten', isFallback: false, status: 'streaming'
    })
    expect(chat.activity.phase).toBe('compacting_context')

    source.emit('context.summary.updated', {
      summary: {
        content: 'Goal\nWeiterarbeiten\n\nFacts\nDer Kontext wächst',
        isFallback: false,
        status: 'streaming'
      }
    })
    expect(chat.streamContextSummary.content).toBe(
      'Goal\nWeiterarbeiten\n\nFacts\nDer Kontext wächst'
    )

    const summary = {
      id: 'summary-1',
      coveredThroughMessageId: 'message-1',
      content: 'Goal\nWeiterarbeiten\n\nFacts\nDer Kontext wächst',
      isFallback: false,
      createdAt: '2026-08-30T10:00:00Z'
    }
    source.emit('context.compacted', { summary })

    expect(chat.streamContextSummary).toBeNull()
    expect(chat.current.contextSummaries).toEqual([summary])
    expect(refresh).toHaveBeenCalledWith('session-1')
  })

  it('keeps a session unread when acknowledging it fails', async () => {
    api.get.mockResolvedValue({
      data: {
        id: 'session-1',
        hasUnreadMessages: true,
        activeRun: null,
        messages: [{ id: 'assistant-message', role: 'assistant' }]
      }
    })
    api.post.mockRejectedValue(new Error('network'))
    const chat = useChatStore()
    setSessions(chat, [{ id: 'session-1', hasUnreadMessages: true }])

    await chat.loadSession('session-1')
    await expect(chat.markSessionRead(chat.current)).rejects.toThrow('network')
    await vi.waitFor(() => expect(api.post).toHaveBeenCalled())

    expect(chat.current.hasUnreadMessages).toBe(true)
    expect(chat.sessions[0].hasUnreadMessages).toBe(true)
  })

  it('does not attach a late send response to a newly selected session', async () => {
    global.EventSource = EventSourceMock
    let resolveSend
    api.post.mockReturnValue(new Promise((resolve) => { resolveSend = resolve }))
    api.get.mockImplementation((url) => Promise.resolve({
      data: url === '/sessions/session-2'
        ? { id: 'session-2', messages: [], activeRun: null }
        : []
    }))
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [] }
    chat.selectedSessionId = 'session-1'
    const watchRun = vi.spyOn(chat, 'watchRun')

    const sending = chat.sendToCurrent('Nachricht an Session 1')
    await chat.loadSession('session-2')
    resolveSend({
      data: {
        runId: 'run-1',
        message: { id: 'message-1', role: 'user', content: 'Nachricht an Session 1' }
      }
    })
    await sending

    expect(chat.current.id).toBe('session-2')
    expect(chat.selectedSessionId).toBe('session-2')
    expect(chat.running).toBe(false)
    expect(watchRun).not.toHaveBeenCalled()
  })

  it('watches the accepted follow-up after a same-session refresh overtakes its send', async () => {
    global.EventSource = EventSourceMock
    let acceptSend
    api.post.mockImplementation((url) => url.endsWith('/messages')
      ? new Promise((resolve) => { acceptSend = resolve })
      : Promise.resolve({ data: {} }))
    api.get.mockResolvedValue({ data: { id: 'session-1', messages: [], activeRun: null } })
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [] }
    chat.selectedSessionId = 'session-1'

    const sending = chat.sendToCurrent('Korrigierte Anfrage')
    await vi.waitFor(() => expect(acceptSend).toBeTypeOf('function'))
    await chat.refreshSession('session-1')
    acceptSend({ data: {
      runId: 'follow-up',
      message: { id: 'correction', role: 'user', content: 'Korrigierte Anfrage' }
    } })
    await sending

    expect(chat.current.messages.map((message) => message.id)).toEqual(['correction'])
    expect(chat.runId).toBe('follow-up')
    expect(chat.eventSource.url).toBe('/api/sessions/session-1/runs/follow-up/events')

    api.get.mockImplementation((url) => Promise.resolve({ data: url === '/sessions/session-1'
      ? {
          id: 'session-1', activeRun: null,
          messages: [
            { id: 'correction', role: 'user', content: 'Korrigierte Anfrage' },
            { id: 'failure', role: 'assistant', kind: 'runtime_error', error: { code: 'provider.unavailable' } }
          ]
        }
      : { items: [], nextCursor: null }
    }))
    EventSourceMock.instances.at(-1).emit('run.failed', { runId: 'follow-up' })
    await vi.waitFor(() => expect(chat.current.messages.at(-1)).toMatchObject({
      kind: 'runtime_error', error: { code: 'provider.unavailable' }
    }))
    expect(chat.running).toBe(false)
  })

  it.each([
    [null, true], [null, false], ['project-1', true], ['project-1', false]
  ])('promotes a continued session in scope %s (already listed: %s)', async (projectId, listed) => {
    api.post.mockResolvedValue({
      data: {
        runId: 'run-1',
        message: {
          id: 'message-1',
          role: 'user',
          content: 'Neue Nachricht',
          createdAt: '2026-07-24T15:00:00Z'
        }
      }
    })
    const chat = useChatStore()
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})
    const list = chat.ensureSessionList(projectId)
    list.loaded = true
    list.items = [
      { id: 'session-2', projectId, lastMessageAt: '2026-07-24T14:00:00Z' },
      ...(listed ? [{ id: 'session-1', projectId, lastMessageAt: '2026-07-20T12:00:00Z' }] : [])
    ]
    chat.current = {
      id: 'session-1',
      projectId,
      lastMessageAt: '2026-07-20T12:00:00Z',
      userRequestCount: 3,
      messages: []
    }
    chat.selectedSessionId = 'session-1'

    await chat.sendToCurrent('Neue Nachricht')

    expect(list.items.map((session) => session.id)).toEqual(['session-1', 'session-2'])
    expect(list.items[0].lastMessageAt).toBe('2026-07-24T15:00:00Z')
    expect(list.items[0]).not.toHaveProperty('messages')
    expect(chat.current.lastMessageAt).toBe('2026-07-24T15:00:00Z')
    expect(chat.current.userRequestCount).toBe(4)
  })

  it.each([null, 'project-1'])('syncs loaded and refreshed chat activity in scope %s without promoting visits', async (projectId) => {
    const chat = useChatStore()
    const list = chat.ensureSessionList(projectId)
    list.loaded = true
    const oldSession = { id: 'session-1', projectId, lastMessageAt: '2026-07-20T12:00:00Z' }
    list.items = [
      { id: 'pinned', projectId, pinned: true, lastMessageAt: '2026-07-01T12:00:00Z' },
      { id: 'session-2', projectId, lastMessageAt: '2026-07-24T14:00:00Z' },
      oldSession
    ]
    chat.sessionCatalog = [...list.items]
    api.get.mockResolvedValue({ data: { ...oldSession, messages: [], activeRun: null } })

    await chat.loadSession('session-1')

    expect(list.items.map((session) => session.id)).toEqual(['pinned', 'session-2', 'session-1'])
    const recentSession = {
      ...oldSession,
      lastMessageAt: '2026-07-24T15:00:00Z',
      messages: [{ id: 'reply', role: 'assistant', createdAt: '2026-07-24T15:00:00Z' }],
      activeRun: null,
      contextSummaries: []
    }
    api.get.mockResolvedValue({ data: recentSession })

    await chat.refreshSession('session-1')

    expect(list.items.map((session) => session.id)).toEqual(['pinned', 'session-1', 'session-2'])
    expect(chat.sessionCatalog.map((session) => session.id)).toEqual(['pinned', 'session-1', 'session-2'])
    expect(list.items[1].lastMessageAt).toBe(recentSession.lastMessageAt)
    expect(chat.sessionCatalog[1].lastMessageAt).toBe(recentSession.lastMessageAt)
    expect(list.items[1]).not.toHaveProperty('messages')
    expect(list.items[1]).not.toHaveProperty('contextSummaries')
    expect(list.items[1]).not.toHaveProperty('activeRun')

    list.items[1] = oldSession
    await chat.loadSession('session-1')
    expect(list.items[1].lastMessageAt).toBe(recentSession.lastMessageAt)
  })

  it('does not navigate back to a late new-session response', async () => {
    let resolveSend
    api.post.mockReturnValue(new Promise((resolve) => { resolveSend = resolve }))
    api.get.mockResolvedValue({
      data: { id: 'session-2', messages: [], activeRun: null }
    })
    const chat = useChatStore()
    const watchRun = vi.spyOn(chat, 'watchRun')

    const sending = chat.startSession('Neue Nachricht')
    await chat.loadSession('session-2')
    resolveSend({
      data: {
        session: { id: 'new-session', title: 'Neue Nachricht' },
        message: { id: 'message-new', role: 'user', content: 'Neue Nachricht' },
        runId: 'run-new'
      }
    })
    const result = await sending

    expect(result).toEqual({ created: false, sessionId: 'new-session' })
    expect(chat.current.id).toBe('session-2')
    expect(chat.selectedSessionId).toBe('session-2')
    expect(watchRun).not.toHaveBeenCalled()
  })

  it('does not merge a late settings response into another session', async () => {
    let resolveUpdate
    api.patch.mockReturnValue(new Promise((resolve) => { resolveUpdate = resolve }))
    const chat = useChatStore()
    chat.current = { id: 'session-1', title: 'Eins' }
    chat.selectedSessionId = 'session-1'

    const updating = chat.updateCurrent({ title: 'Neu' })
    chat.current = { id: 'session-2', title: 'Zwei' }
    chat.selectedSessionId = 'session-2'
    resolveUpdate({ data: { id: 'session-1', title: 'Neu' } })
    await updating

    expect(chat.current).toEqual({ id: 'session-2', title: 'Zwei' })
  })

  it('keeps the latest session loading when an aborted request rejects', async () => {
    const requests = {}
    api.get.mockImplementation((url, config) => new Promise((resolve, reject) => {
      requests[url] = { resolve, reject, config }
    }))
    const chat = useChatStore()

    const firstLoad = chat.loadSession('session-1')
    const secondLoad = chat.loadSession('session-2')
    const refreshing = chat.refreshSession('session-2')
    expect(requests['/sessions/session-2'].config.signal.aborted).toBe(false)
    requests['/sessions/session-1'].reject(new Error('aborted'))
    await firstLoad

    expect(chat.loading).toBe(true)
    expect(chat.loadError).toBeNull()
    requests['/sessions/session-2'].resolve({
      data: { id: 'session-2', messages: [], activeRun: null }
    })
    await secondLoad
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledTimes(3))
    requests['/sessions/session-2'].resolve({
      data: { id: 'session-2', messages: [], activeRun: null }
    })
    await refreshing

    expect(chat.current.id).toBe('session-2')
    expect(chat.loading).toBe(false)
  })

  it('resumes the active run when loading a session', async () => {
    global.EventSource = EventSourceMock
    api.get.mockResolvedValue({
      data: {
        id: 'session-1',
        messages: [],
        activeRun: {
          id: 'run-1',
          status: 'running',
          modelName: 'Aktives Modell',
          createdAt: '2026-07-19T12:00:00Z',
          activeDurationMs: 1_500,
          activeDurationRunning: true,
          activeTimingRevision: 1
        }
      }
    })
    const chat = useChatStore()

    await chat.loadSession('session-1')

    expect(chat.running).toBe(true)
    expect(chat.streamModelName).toBe('Aktives Modell')
    expect(chat.eventSource.url).toBe('/api/sessions/session-1/runs/run-1/events')
    expect(chat.activity).toMatchObject({
      phase: 'preparing_context',
      startedAt: '2026-07-19T12:00:00Z',
      activeDurationMs: 1_500
    })
    expect(chat.activity.activeDurationUpdatedAt).toEqual(expect.any(String))
  })

  it('tracks model, streaming and interaction phases from SSE', () => {
    global.EventSource = EventSourceMock
    api.get.mockResolvedValue(sessionPage())
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [] }
    setSessions(chat, [{ id: 'session-1', title: 'Erste Nachricht' }])
    chat.selectedSessionId = 'session-1'

    chat.watchRun('session-1', 'run-1', {
      status: 'queued',
      createdAt: '2026-07-19T12:00:00Z'
    })
    expect(chat.sessions[0].activeRunStatus).toBe('queued')
    const source = EventSourceMock.instances[0]
    source.emit('run.started', {
      modelName: 'Streaming-Modell',
      createdAt: '2026-07-19T12:00:00Z',
      activeDurationMs: 0,
      activeDurationRunning: true,
      activeTimingRevision: 1
    })
    expect(chat.activity.phase).toBe('preparing_context')
    expect(chat.streamModelName).toBe('Streaming-Modell')
    expect(chat.sessions[0].activeRunStatus).toBe('running')

    source.emit('run.phase', {
      phase: 'waiting_model',
      modelRequest: 1,
      providerAttempt: 2,
      providerAttemptTotal: 3,
      activityLabel: 'Verarbeite Anfrage'
    })
    expect(chat.activity).toMatchObject({
      phase: 'waiting_model',
      modelRequest: 1,
      providerAttempt: 2,
      providerAttemptTotal: 3,
      activityLabel: 'Verarbeite Anfrage'
    })

    source.emit('reasoning.updated', {
      reasoning: { kind: 'summary', text: '**Plan prüfen**', status: 'streaming' },
      activityLabel: 'Plan prüfen'
    })
    expect(chat.streamReasoning.text).toBe('**Plan prüfen**')
    expect(chat.activity.reasoningLabel).toBe('Plan prüfen')

    source.emit('reasoning.updated', {
      reasoning: { kind: 'content', text: 'Weitere Überlegung', status: 'streaming' },
      activityLabel: 'Nachdenken'
    })
    source.emit('run.phase', {
      phase: 'waiting_model', modelRequest: 1, activityLabel: 'Verarbeite Anfrage'
    })
    expect(chat.activity.reasoningLabel).toBe('Plan prüfen')

    source.emit('message.delta', { delta: 'Hallo' })
    expect(chat.activity.phase).toBe('streaming')
    expect(chat.streamContent).toBe('Hallo')

    source.emit('session.title.updated', {
      sessionId: 'session-1',
      title: 'Generierter Titel'
    })
    expect(chat.current.title).toBe('Generierter Titel')
    expect(chat.current.titleGenerationStatus).toBe('completed')
    expect(chat.sessions[0].title).toBe('Generierter Titel')
    expect(chat.sessions[0].titleGenerationStatus).toBe('completed')

    source.emit('approval.created', {
      approval: { id: 'approval-1', details: { tool: 'write_file', toolCallId: 'call-1' } }
    })
    expect(chat.activity).toMatchObject({
      phase: 'waiting_approval',
      tool: 'write_file',
      toolCallId: 'call-1'
    })
    expect(chat.sessions[0].activeRunStatus).toBe('waiting_approval')

    source.emit('run.timing', {
      activeDurationMs: 2_500,
      activeDurationRunning: false,
      activeTimingRevision: 2
    })
    expect(chat.activity).toMatchObject({ activeDurationMs: 2_500, activeDurationUpdatedAt: null })
    source.emit('run.timing', {
      activeDurationMs: 0,
      activeDurationRunning: true,
      activeTimingRevision: 1
    })
    expect(chat.activity).toMatchObject({ activeDurationMs: 2_500, activeDurationUpdatedAt: null })

    source.emit('approval.resolved', {})
    expect(chat.sessions[0].activeRunStatus).toBe('running')
    source.emit('run.timing', {
      activeDurationMs: 2_500,
      activeDurationRunning: true,
      activeTimingRevision: 3
    })
    expect(chat.activity.activeDurationUpdatedAt).toEqual(expect.any(String))

    source.emit('question.created', {
      question: { id: 'question-1', toolCallId: 'call-2', questions: [] }
    })
    expect(chat.activity).toMatchObject({
      phase: 'waiting_input',
      tool: 'question',
      toolCallId: 'call-2'
    })
    expect(chat.sessions[0].activeRunStatus).toBe('waiting_input')
    expect(useQuestionStore().pending).toHaveLength(1)

    source.emit('question.rejected', {})
    expect(chat.sessions[0].activeRunStatus).toBe('running')
  })

  it('promotes a live runtime warning into the completed message', () => {
    global.EventSource = EventSourceMock
    api.get.mockResolvedValue(sessionPage())
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [] }
    chat.selectedSessionId = 'session-1'
    chat.watchRun('session-1', 'run-1', { status: 'running' })
    const source = EventSourceMock.instances[0]

    const warning = {
      code: 'privacy.placeholderRestorationIncomplete',
      modelRequest: 1
    }
    source.emit('run.warning', warning)
    source.emit('run.warning', warning)

    expect(chat.runWarnings).toEqual([warning])

    source.emit('message.completed', {
      message: {
        id: 'assistant-1', role: 'assistant', content: 'Antwort', toolCalls: [],
        warnings: ['privacy.placeholderRestorationIncomplete']
      }
    })

    expect(chat.runWarnings).toEqual([])
    expect(chat.current.messages[0].warnings).toEqual([
      'privacy.placeholderRestorationIncomplete'
    ])
  })

  it('replaces cumulative process output snapshots from SSE', () => {
    global.EventSource = EventSourceMock
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [] }
    chat.selectedSessionId = 'session-1'
    chat.watchRun('session-1', 'run-1', { status: 'running' })
    const source = EventSourceMock.instances[0]

    source.emit('process.output', { toolCallId: 'call-1', stdout: 'eins', stderr: '' })
    source.emit('process.output', { toolCallId: 'call-1', stdout: 'eins\nzwei', stderr: 'warnung' })

    expect(chat.processOutputs['call-1']).toEqual({
      stdout: 'eins\nzwei',
      stderr: 'warnung'
    })
  })

  it('replaces todos live and hides terminal lists only on the next run, not reconnect', () => {
    global.EventSource = EventSourceMock
    const chat = useChatStore()
    chat.current = {
      id: 'session-1', messages: [], todos: [{ content: 'Alt', status: 'cancelled' }],
      latestRunId: 'run-0', todosCollapsed: true
    }
    chat.selectedSessionId = 'session-1'
    chat.watchRun('session-1', 'run-1', { status: 'running' })
    expect(chat.current.todos).toEqual([])

    EventSourceMock.instances[0].emit('todo.updated', {
      todos: [{ content: 'Prüfen', status: 'in_progress' }]
    })

    expect(chat.current.todos).toEqual([{ content: 'Prüfen', status: 'in_progress' }])
    const completed = [{ content: 'Prüfen', status: 'completed' }]
    EventSourceMock.instances[0].emit('todo.updated', { todos: completed })
    chat.watchRun('session-1', 'run-1', { status: 'running', reconnecting: true })
    expect(chat.current.todos).toEqual(completed)
    chat.watchRun('session-1', 'run-2', { status: 'queued' })
    expect(chat.current.todos).toEqual([])
    expect(chat.current.todosCollapsed).toBe(true)
  })

  it.each(['pending', 'in_progress'])('carries %s todos into the next run', (status) => {
    global.EventSource = EventSourceMock
    const chat = useChatStore()
    const todos = [{ content: 'Offen', status }, { content: 'Fertig', status: 'completed' }]
    chat.current = { id: 'session-1', messages: [], todos, latestRunId: 'run-0' }
    chat.watchRun('session-1', 'run-1')
    expect(chat.current.todos).toEqual(todos)
  })

  it('saves the collapse preference without replacing live state or another session', async () => {
    const chat = useChatStore()
    chat.current = { id: 'session-1', todosCollapsed: false, todos: [] }
    let resolve
    api.patch.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const saving = chat.setTodosCollapsed('session-1', true)
    const todos = [{ content: 'Live', status: 'in_progress' }]
    chat.current.todos = todos
    expect(chat.current.todosCollapsed).toBe(false)
    resolve({ data: { id: 'session-1', todosCollapsed: true } })
    await saving
    expect(api.patch).toHaveBeenCalledWith('/sessions/session-1', { todos_collapsed: true })
    expect(chat.current.todosCollapsed).toBe(true)
    expect(chat.current.todos).toEqual(todos)

    api.patch.mockRejectedValueOnce(new Error('offline'))
    await expect(chat.setTodosCollapsed('session-1', false)).rejects.toThrow('offline')
    expect(chat.current.todosCollapsed).toBe(true)

    api.patch.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const savingPreviousSession = chat.setTodosCollapsed('session-1', false)
    chat.current = { id: 'session-2', todosCollapsed: true }
    resolve({ data: { id: 'session-1', todosCollapsed: false } })
    await savingPreviousSession
    expect(chat.current).toEqual({ id: 'session-2', todosCollapsed: true })
  })

  it('refreshes session files after a successful filesystem tool completes', () => {
    global.EventSource = EventSourceMock
    api.get.mockResolvedValue({ data: { id: 'session-1', messages: [] } })
    const workspace = useWorkspaceStore()
    workspace.sessionId = 'session-1'
    workspace.rootIdentity = 'session-1'
    const refresh = vi.spyOn(workspace, 'refresh').mockResolvedValue([])
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [] }
    chat.selectedSessionId = 'session-1'
    chat.watchRun('session-1', 'run-1', { status: 'running' })
    const source = EventSourceMock.instances[0]

    source.emit('tool.completed', {
      tool: 'write_file',
      metadata: {},
      output: '{"root":"session","written":"live.txt"}'
    })
    source.emit('tool.completed', {
      tool: 'edit_file',
      metadata: {},
      output: '{"root":"session","edited":"live.txt"}'
    })
    source.emit('tool.completed', {
      tool: 'write_file',
      metadata: { errorCode: 'permission.denied' },
      output: '{}'
    })
    source.emit('tool.completed', {
      tool: 'read_file',
      metadata: {},
      output: '{}'
    })

    expect(refresh).toHaveBeenCalledTimes(2)
  })

  it('keeps final file actions closed until explicitly opened', async () => {
    global.EventSource = EventSourceMock
    const sessionId = '550e8400-e29b-41d4-a716-446655440000'
    const rootIdentity = sessionId
    const action = {
      operation: 'write', root: 'session', rootIdentity,
      path: 'result.md', filename: 'result.md', sizeBytes: 7,
      downloadUrl: `/api/workspace/files/download?path=result.md&scope=session&session_id=${sessionId}&root_identity=${rootIdentity}`
    }
    api.get.mockResolvedValue({
      data: {
        id: sessionId,
        messages: [
          { role: 'assistant', toolCalls: [{ id: 'call-1', name: 'write_file', arguments: { root: 'files', path: 'result.md', content: 'Fertig' } }] },
          { role: 'tool', runId: 'run-1', toolCallId: 'call-1', fileActions: [action] }
        ]
      }
    })
    const workspace = useWorkspaceStore()
    workspace.sessionId = sessionId
    workspace.scope = 'session'
    workspace.rootIdentity = sessionId
    const controlCenter = useControlCenterStore()
    const open = vi.spyOn(controlCenter, 'openFile').mockResolvedValue('file-result')
    const chat = useChatStore()
    chat.current = { id: sessionId, messages: [] }
    chat.selectedSessionId = sessionId
    chat.watchRun(sessionId, 'run-1', { status: 'running' })
    const source = EventSourceMock.instances[0]

    source.emit('run.phase', { phase: 'processing_tool', tool: 'write_file', toolCallId: 'call-1' })
    source.emit('tool.completed', { tool: 'write_file', toolCallId: 'call-1', metadata: {} })

    expect(open).not.toHaveBeenCalled()
    expect(chat.completedToolCallId).toBe('call-1')
    source.emit('run.completed', {})
    await vi.waitFor(() => expect(chat.current.messages).toHaveLength(2))
    expect(open).not.toHaveBeenCalled()
  })

  it('keeps a dirty edit active when a managed attachment arrives', async () => {
    global.EventSource = EventSourceMock
    const sessionId = '550e8400-e29b-41d4-a716-446655440000'
    const rootIdentity = sessionId
    api.get.mockResolvedValue({
      data: {
        id: sessionId,
        projectId: rootIdentity,
        messages: [{
          role: 'tool',
          runId: 'run-1',
          toolCallId: 'call-1',
          attachments: [{
            kind: 'file',
            filename: 'generated.bin',
            sizeBytes: 12,
            fileRootId: rootIdentity,
            fileId: '550e8400-e29b-41d4-a716-446655440008',
            fileRevision: 1,
            filePath: 'generated.bin'
          }]
        }]
      }
    })
    const workspace = useWorkspaceStore()
    workspace.sessionId = sessionId
    workspace.scope = 'session'
    workspace.rootIdentity = rootIdentity
    workspace.openEntry({ kind: 'file', name: 'notes.md', path: 'notes.md' }, true)
    workspace.updateEditorDraft('lokal', 'alt')
    const controlCenter = useControlCenterStore()
    const open = vi.spyOn(controlCenter, 'openFile').mockResolvedValue('file-generated')
    const chat = useChatStore()
    chat.current = { id: sessionId, projectId: rootIdentity, messages: [] }
    chat.selectedSessionId = sessionId
    chat.watchRun(sessionId, 'run-1', { status: 'running' })
    const source = EventSourceMock.instances[0]

    source.emit('run.phase', { phase: 'processing_tool', tool: 'generate_file', toolCallId: 'call-1' })
    source.emit('tool.completed', { tool: 'generate_file', toolCallId: 'call-1', metadata: {} })
    source.emit('run.completed', {})

    await vi.waitFor(() => expect(chat.current.messages).toHaveLength(1))
    expect(open).not.toHaveBeenCalled()
    expect(workspace.editorDraft).toBe('lokal')
  })

  it('refreshes an open managed attachment update without auto-opening another file', async () => {
    global.EventSource = EventSourceMock
    const sessionId = '550e8400-e29b-41d4-a716-446655440000'
    const rootIdentity = sessionId
    api.get.mockResolvedValue({
      data: {
        id: sessionId,
        projectId: rootIdentity,
        messages: [{
          role: 'tool',
          runId: 'run-1',
          toolCallId: 'call-1',
          attachments: [{
            kind: 'file',
            filename: 'generated.bin',
            sizeBytes: 13,
            fileRootId: rootIdentity,
            fileId: '550e8400-e29b-41d4-a716-446655440008',
            fileRevision: 2,
            filePath: 'generated.bin'
          }]
        }]
      }
    })
    const workspace = useWorkspaceStore()
    workspace.sessionId = sessionId
    workspace.scope = 'session'
    workspace.rootIdentity = rootIdentity
    workspace.openEntry({ kind: 'file', name: 'generated.bin', path: 'generated.bin' }, true)
    workspace.editorDraft = 'stale'
    const controlCenter = useControlCenterStore()
    const open = vi.spyOn(controlCenter, 'openFile').mockResolvedValue('file-generated')
    const chat = useChatStore()
    chat.current = { id: sessionId, projectId: rootIdentity, messages: [] }
    chat.selectedSessionId = sessionId
    chat.watchRun(sessionId, 'run-1', { status: 'running' })
    const source = EventSourceMock.instances[0]

    source.emit('run.phase', { phase: 'processing_tool', tool: 'generate_file', toolCallId: 'call-1' })
    source.emit('tool.completed', { tool: 'generate_file', toolCallId: 'call-1', metadata: {} })

    source.emit('run.completed', {})
    await vi.waitFor(() => expect(workspace.fileChangeVersions['generated.bin']).toBe(1))
    expect(workspace.editorDraft).toBeNull()
    expect(open).not.toHaveBeenCalled()
  })

  it('keeps the tool completion refresh alive when message completion follows', async () => {
    global.EventSource = EventSourceMock
    const sessionId = '550e8400-e29b-41d4-a716-446655440000'
    const rootIdentity = sessionId
    const action = {
      operation: 'write', root: 'session', rootIdentity,
      path: 'result.md', filename: 'result.md', sizeBytes: 7,
      downloadUrl: `/api/workspace/files/download?path=result.md&scope=session&session_id=${sessionId}&root_identity=${rootIdentity}`
    }
    let resolveToolRefresh
    let sessionRefreshCalls = 0
    api.get.mockImplementation((url) => {
      if (url !== `/sessions/${sessionId}`) return Promise.resolve({ data: { entries: [] } })
      sessionRefreshCalls += 1
      return sessionRefreshCalls === 1
        ? new Promise((resolve) => { resolveToolRefresh = resolve })
        : Promise.resolve({ data: { id: sessionId, messages: [] } })
    })
    const workspace = useWorkspaceStore()
    workspace.sessionId = sessionId
    workspace.scope = 'session'
    workspace.rootIdentity = rootIdentity
    const controlCenter = useControlCenterStore()
    const open = vi.spyOn(controlCenter, 'openFile').mockResolvedValue('file-result')
    const chat = useChatStore()
    chat.current = { id: sessionId, projectId: rootIdentity, messages: [] }
    chat.selectedSessionId = sessionId
    chat.watchRun(sessionId, 'run-1', { status: 'running' })
    const source = EventSourceMock.instances[0]

    source.emit('run.phase', { phase: 'processing_tool', tool: 'write_file', toolCallId: 'call-1' })
    source.emit('tool.completed', { tool: 'write_file', toolCallId: 'call-1', metadata: {} })
    await vi.waitFor(() => expect(sessionRefreshCalls).toBe(1))
    source.emit('message.completed', {
      message: { id: 'assistant-1', role: 'assistant', content: 'Fertig', toolCalls: [] }
    })
    await vi.waitFor(() => expect(sessionRefreshCalls).toBe(1))
    const sessionRequests = () => api.get.mock.calls.filter(([url]) => url === `/sessions/${sessionId}`)
    expect(sessionRequests()[0][1].signal.aborted).toBe(false)
    resolveToolRefresh({
      data: {
        id: sessionId,
        projectId: rootIdentity,
        messages: [
          { role: 'assistant', toolCalls: [{ id: 'call-1', name: 'write_file', arguments: { root: 'files', path: 'result.md', content: 'Fertig' } }] },
          { role: 'tool', toolCallId: 'call-1', fileActions: [action] }
        ]
      }
    })

    await vi.waitFor(() => expect(sessionRefreshCalls).toBe(2))
    expect(open).not.toHaveBeenCalled()
    expect(sessionRequests()[1][1].signal.aborted).toBe(false)
  })

  it('keeps a dirty open file and warns when the agent mutates it', async () => {
    global.EventSource = EventSourceMock
    const sessionId = '550e8400-e29b-41d4-a716-446655440000'
    const rootIdentity = sessionId
    const action = {
      operation: 'write', root: 'session', rootIdentity,
      path: 'result.md', filename: 'result.md', sizeBytes: 7,
      downloadUrl: `/api/workspace/files/download?path=result.md&scope=session&session_id=${sessionId}&root_identity=${rootIdentity}`
    }
    api.get.mockResolvedValue({
      data: { id: sessionId, messages: [{ role: 'tool', runId: 'run-1', toolCallId: 'call-1', fileActions: [action] }] }
    })
    const workspace = useWorkspaceStore()
    workspace.sessionId = sessionId
    workspace.scope = 'session'
    workspace.rootIdentity = rootIdentity
    workspace.openEntry({ kind: 'file', name: 'result.md', path: 'result.md' }, true)
    workspace.updateEditorDraft('lokal', 'alt')
    const chat = useChatStore()
    chat.current = { id: sessionId, messages: [] }
    chat.selectedSessionId = sessionId
    chat.watchRun(sessionId, 'run-1', { status: 'running' })
    const source = EventSourceMock.instances[0]

    source.emit('run.phase', { phase: 'processing_tool', tool: 'edit_file', toolCallId: 'call-1' })
    source.emit('tool.completed', { tool: 'edit_file', toolCallId: 'call-1', metadata: {} })

    source.emit('run.completed', {})
    await vi.waitFor(() => expect(workspace.editorExternalChange).toBe(true))
    expect(workspace.editorDraft).toBe('lokal')
    expect(workspace.editorDirty).toBe(true)
  })

  it('keeps a dirty open file when the agent deletes it', async () => {
    global.EventSource = EventSourceMock
    const sessionId = '550e8400-e29b-41d4-a716-446655440000'
    const rootIdentity = sessionId
    api.get.mockResolvedValue({
      data: {
        id: sessionId,
        projectId: rootIdentity,
        messages: [
          {
            role: 'assistant',
            toolCalls: [{
              id: 'call-1',
              name: 'delete_path',
              arguments: { root: 'files', path: 'result.md' }
            }]
          },
          { role: 'tool', toolCallId: 'call-1' }
        ]
      }
    })
    const workspace = useWorkspaceStore()
    workspace.sessionId = sessionId
    workspace.scope = 'session'
    workspace.rootIdentity = rootIdentity
    workspace.openEntry({ kind: 'file', name: 'result.md', path: 'result.md' }, true)
    workspace.updateEditorDraft('lokal', 'alt')
    const chat = useChatStore()
    chat.current = { id: sessionId, projectId: rootIdentity, messages: [] }
    chat.selectedSessionId = sessionId
    chat.watchRun(sessionId, 'run-1', { status: 'running' })
    const source = EventSourceMock.instances[0]

    source.emit('run.phase', { phase: 'processing_tool', tool: 'delete_path', toolCallId: 'call-1' })
    source.emit('tool.completed', { tool: 'delete_path', toolCallId: 'call-1', metadata: {} })

    await vi.waitFor(() => expect(workspace.editorExternalChange).toBe(true))
    expect(workspace.editorDraft).toBe('lokal')
  })

  it('does not replace a newer run status with a stale session list', async () => {
    let resolveSessions
    api.get.mockImplementationOnce(() => new Promise((resolve) => {
      resolveSessions = resolve
    }))
    const chat = useChatStore()
    setSessions(chat, [{ id: 'session-1', title: 'Test', activeRunStatus: null }])

    const request = chat.fetchSessions()
    chat.setSessionRunStatus('session-1', 'running')
    resolveSessions(sessionPage([{ id: 'session-1', title: 'Test', activeRunStatus: null }]))
    await request

    expect(chat.sessions[0].activeRunStatus).toBe('running')
  })

  it('appends paginated sessions without duplicates and stops at the end', async () => {
    api.get
      .mockResolvedValueOnce(sessionPage([
        { id: 'session-1', title: 'Neu', lastMessageAt: '2026-07-20T12:00:00Z' },
        { id: 'session-2', title: 'Alt', lastMessageAt: '2026-07-19T12:00:00Z' }
      ], 'cursor-1'))
      .mockResolvedValueOnce(sessionPage([
        { id: 'session-2', title: 'Aktualisiert', lastMessageAt: '2026-07-19T12:00:00Z' },
        { id: 'session-3', title: 'Älter', lastMessageAt: '2026-07-18T12:00:00Z' }
      ]))
    const chat = useChatStore()

    await chat.fetchSessions()
    await chat.fetchMoreSessions()

    expect(chat.sessions.map((session) => session.id)).toEqual([
      'session-1', 'session-2', 'session-3'
    ])
    expect(chat.sessions[1].title).toBe('Aktualisiert')
    expect(chat.activeSessionList.nextCursor).toBeNull()
    expect(api.get).toHaveBeenNthCalledWith(2, '/sessions', {
      params: { standalone: true, pageSize: 20, cursor: 'cursor-1' }
    })
  })

  it('preserves an in-flight list page when a new session is created', async () => {
    let resolveList
    api.get.mockReturnValue(new Promise((resolve) => { resolveList = resolve }))
    api.post.mockResolvedValue({
      data: {
        session: { id: 'new-session', projectId: null, title: 'Neu', messages: [] },
        runId: 'run-1'
      }
    })
    const chat = useChatStore()
    vi.spyOn(chat, 'watchRun').mockImplementation(() => {})

    const loading = chat.fetchSessions()
    const creating = chat.startSession('Neue Session')
    resolveList(sessionPage([{ id: 'old-session', projectId: null }], 'next-page'))
    await Promise.all([loading, creating])

    expect(chat.sessions.map((session) => session.id)).toEqual(['new-session', 'old-session'])
    expect(chat.activeSessionList.nextCursor).toBe('next-page')
  })

  it('scopes session lists and new-session drafts by project', async () => {
    api.get.mockResolvedValue(sessionPage([]))
    const chat = useChatStore()
    chat.setDraft('Ohne Projekt')

    await chat.setSessionScope('project-1')
    chat.setDraft('Projektentwurf')
    await chat.setSessionScope(null)

    expect(chat.activeDraft).toBe('Ohne Projekt')
    await chat.setSessionScope('project-1')
    expect(chat.activeDraft).toBe('Projektentwurf')
    expect(api.get).toHaveBeenNthCalledWith(1, '/sessions', {
      params: { projectId: 'project-1', pageSize: 20 }
    })
    expect(api.get).toHaveBeenNthCalledWith(2, '/sessions', {
      params: { standalone: true, pageSize: 20 }
    })
  })

  it('moves new-session text, attachments and recovery audio into an empty project draft', async () => {
    api.get.mockResolvedValue(sessionPage([]))
    const chat = useChatStore()
    const attachment = new File(['content'], 'entwurf.txt', { type: 'text/plain' })
    const recording = new File(['audio'], 'aufnahme.wav', { type: 'audio/wav' })
    chat.setDraft('Mitnehmen')
    chat.addAttachmentFiles([attachment])
    chat.setAudioDictation({ files: [recording] })
    await chat.setSessionScope('project-1')
    chat.moveNewSessionDraft(null, 'project-1')
    expect({ message: chat.activeDraft, files: chat.activeAttachmentDraft.map(item => item.file), audio: chat.getAudioDictation()?.files, sourceOccupied: chat.hasNewSessionDraft(null) })
      .toEqual({ message: 'Mitnehmen', files: [attachment], audio: [recording], sourceOccupied: false })
  })

  it.each(['text', 'attachment', 'recording', 'queue'])('does not overwrite a destination containing %s', async (kind) => {
    api.get.mockResolvedValue(sessionPage([]))
    const chat = useChatStore()
    chat.setDraft('Bisheriger Entwurf')
    await chat.setSessionScope('project-1')
    if (kind === 'text') chat.setDraft('Vorhandener Entwurf')
    if (kind === 'attachment') chat.addAttachmentFiles([new File(['content'], 'vorhanden.txt', { type: 'text/plain' })])
    if (kind === 'recording') chat.setAudioDictation({ files: [new File(['audio'], 'aufnahme.wav')] })
    if (kind === 'queue') chat.queuedMessages['new:project-1'] = [{ id: 'queued' }]
    expect([chat.moveNewSessionDraft(null, 'project-1'), chat.drafts.newSessions.standalone.message, chat.hasNewSessionDraft('project-1')])
      .toEqual([false, 'Bisheriger Entwurf', true])
  })

  it('shares an in-flight page request across callers', async () => {
    let resolvePage
    api.get.mockReturnValue(new Promise((resolve) => { resolvePage = resolve }))
    const chat = useChatStore()
    setSessions(chat, [{ id: 'session-1' }], { nextCursor: 'cursor-1' })

    const loadingPage = chat.fetchMoreSessions()
    const duplicateRequest = chat.fetchMoreSessions()
    resolvePage(sessionPage([{ id: 'session-2' }]))
    await Promise.all([loadingPage, duplicateRequest])

    expect(api.get).toHaveBeenCalledOnce()
    expect(chat.activeSessionList.loadingMore).toBe(false)
    expect(chat.activeSessionList.nextCursor).toBeNull()
  })

  it('replaces stale loaded summaries when refreshing the visible depth', async () => {
    api.get.mockResolvedValue(sessionPage([{ id: 'session-1', title: 'Aktuell' }]))
    const chat = useChatStore()
    setSessions(chat, [{ id: 'session-1', title: 'Alt' }, { id: 'deleted' }])

    await chat.refreshSessions()

    expect(chat.sessions).toEqual([{ id: 'session-1', title: 'Aktuell' }])
  })

  it('keeps a list update when a concurrent session reload changes its version', async () => {
    let finishList
    const updated = sessionPage([{ id: 'session-1' }, { id: 'session-2', title: 'Neu' }])
    let listCalls = 0
    api.get.mockImplementation((url) => url !== '/sessions'
      ? Promise.resolve({ data: { id: 'session-1', messages: [] } })
      : ++listCalls === 1 ? new Promise((resolve) => { finishList = resolve }) : Promise.resolve(updated))
    const chat = useChatStore()
    setSessions(chat, [{ id: 'session-1' }, { id: 'session-2', title: 'Alt' }])
    chat.current = { id: 'session-1', messages: [] }
    chat.selectedSessionId = 'session-1'
    const detail = chat.refreshSession('session-1')
    const list = chat.refreshSessions()
    await detail
    finishList(updated)
    await list
    expect(chat.sessions.find((session) => session.id === 'session-2').title).toBe('Neu')
  })

  it('keeps a list update received during its initial load', async () => {
    let finishLoad
    api.get.mockReturnValueOnce(new Promise((resolve) => { finishLoad = resolve }))
      .mockResolvedValue(sessionPage([{ id: 'session-1', title: 'Neu' }]))
    const chat = useChatStore()
    const loading = chat.fetchSessions()
    const refreshing = chat.refreshSessions()
    finishLoad(sessionPage([{ id: 'session-1', title: 'Alt' }]))
    await Promise.all([loading, refreshing])
    expect(chat.sessions[0].title).toBe('Neu')
  })

  it('batches queued list refreshes without reusing a read from before the change', async () => {
    let finishFirst
    api.get.mockReturnValueOnce(new Promise((resolve) => { finishFirst = resolve }))
      .mockResolvedValue(sessionPage([{ id: 'session-1', title: 'Aktuell' }]))
    const chat = useChatStore()
    setSessions(chat, [{ id: 'session-1', title: 'Alt' }])
    const first = chat.refreshSessions()
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledOnce())
    const second = chat.refreshSessions()
    const third = chat.refreshSessions()
    finishFirst(sessionPage([{ id: 'session-1', title: 'Alt' }]))
    await Promise.all([first, second, third])
    expect([api.get.mock.calls.length, chat.sessions[0].title]).toEqual([2, 'Aktuell'])
  })

  it('promotes canonical completed content before refreshing the persisted message', async () => {
    global.EventSource = EventSourceMock
    let resolveRefresh
    api.get.mockImplementation(() => new Promise((resolve) => {
      resolveRefresh = resolve
    }))
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [] }
    chat.selectedSessionId = 'session-1'
    chat.watchRun('session-1', 'run-1', { status: 'running' })
    const source = EventSourceMock.instances[0]

    source.emit('message.delta', { delta: 'Vollständige Antwort' })
    source.emit('message.completed', {
      message: {
        id: 'message-1',
        role: 'assistant',
        content: 'Abgeschnitten',
        createdAt: '2026-07-25T14:32:00Z',
        modelName: 'Antwortmodell',
        research: {
          policy: 'available',
          used: true,
          linkedSources: [{ title: 'Quelle', url: 'https://example.test/source' }]
        },
        toolCalls: []
      }
    })

    expect(chat.streamContent).toBe('')
    expect(chat.current.messages[0]).toMatchObject({
      id: 'message-1',
      content: 'Abgeschnitten',
      createdAt: '2026-07-25T14:32:00Z',
      modelName: 'Antwortmodell',
      research: {
        linkedSources: [{ title: 'Quelle', url: 'https://example.test/source' }]
      }
    })
    expect(chat.streamModelName).toBe('Antwortmodell')
    source.emit('message.delta', { delta: 'Vorläufig' })
    source.emit('reasoning.updated', { reasoning: { text: 'Vorläufige Begründung' } })
    source.emit('message.completed', {
      message: { id: 'message-2', role: 'assistant', content: '', reasoning: null, toolCalls: [] }
    })
    expect(chat.current.messages[1]).toMatchObject({ content: '', reasoning: null })
    await vi.waitFor(() => expect(resolveRefresh).toBeTypeOf('function'))
    resolveRefresh({
      data: {
        id: 'session-1',
        messages: [{ id: 'message-1', role: 'assistant', content: 'Serverstand' }]
      }
    })
    await vi.waitFor(() => expect(chat.current.messages[0].content).toBe('Serverstand'))
  })

  it('refreshes a generated title after the first run', async () => {
    vi.useFakeTimers()
    api.get.mockResolvedValue(sessionPage([{
        id: 'session-1',
        title: 'Standardmodelle konfigurieren',
        titleGenerationStatus: 'completed'
      }]))
    const chat = useChatStore()
    chat.current = {
      id: 'session-1',
      title: 'Default Models einbauen',
      titleGenerationStatus: 'running',
      messages: []
    }
    setSessions(chat, [{
      id: 'session-1',
      title: 'Default Models einbauen',
      titleGenerationStatus: 'running'
    }])

    const refresh = chat.refreshGeneratedTitle('session-1')
    await vi.advanceTimersByTimeAsync(1000)
    await refresh

    expect(chat.current.title).toBe('Standardmodelle konfigurieren')
    expect(chat.current.titleGenerationStatus).toBe('completed')
    expect(api.get).toHaveBeenCalledWith('/sessions', {
      params: { standalone: true, pageSize: 20 }
    })
  })

  it('stops generated title polling after a terminal failure', async () => {
    vi.useFakeTimers()
    api.get.mockResolvedValue(sessionPage([{
        id: 'session-1',
        title: 'Lokaler Titel',
        titleGenerationStatus: 'failed'
      }]))
    const chat = useChatStore()
    chat.current = {
      id: 'session-1',
      title: 'Lokaler Titel',
      titleGenerationStatus: 'running',
      messages: []
    }
    setSessions(chat, [{
      id: 'session-1',
      title: 'Lokaler Titel',
      titleGenerationStatus: 'running'
    }])

    const refresh = chat.refreshGeneratedTitle('session-1')
    await vi.advanceTimersByTimeAsync(1000)
    await refresh

    expect(chat.current.titleGenerationStatus).toBe('failed')
    expect(api.get).toHaveBeenCalledTimes(1)
  })

  it('renames a session and keeps the list and current session in sync', async () => {
    api.patch.mockResolvedValue({
      data: {
        id: 'session-1',
        title: 'Neuer Titel',
        updatedAt: '2026-07-20T14:00:00Z'
      }
    })
    const chat = useChatStore()
    setSessions(chat, [
      { id: 'session-2', title: 'Andere Session', lastMessageAt: '2026-07-20T13:00:00Z' },
      { id: 'session-1', title: 'Alter Titel', lastMessageAt: '2026-07-19T13:00:00Z' }
    ])
    chat.current = { id: 'session-1', title: 'Alter Titel', messages: [] }

    await chat.renameSession('session-1', 'Neuer Titel')

    expect(api.patch).toHaveBeenCalledWith('/sessions/session-1', { title: 'Neuer Titel' })
    expect(chat.sessions.map((session) => session.id)).toEqual(['session-2', 'session-1'])
    expect(chat.current.title).toBe('Neuer Titel')
  })

  it('moves a session out of the visible scope after the server succeeds', async () => {
    api.patch.mockResolvedValue({
      data: { id: 'session-1', title: 'Session', projectId: 'project-2' }
    })
    const chat = useChatStore()
    setSessions(chat, [{ id: 'session-1', title: 'Session', projectId: 'project-1' }])
    chat.current = { id: 'session-1', title: 'Session', projectId: 'project-1', messages: [] }

    await chat.moveSession('session-1', 'project-2')

    expect(api.patch).toHaveBeenCalledWith('/sessions/session-1', {
      project_id: 'project-2'
    })
    expect(chat.sessions).toEqual([])
    expect(chat.current.projectId).toBe('project-2')
  })

  it.each([
    [null, 'project-2'], ['project-1', 'project-2'], ['project-1', null]
  ])('preserves chat recency when moving from %s to %s', async (sourceId, targetId) => {
    const chat = useChatStore()
    const session = {
      id: 'session-1', projectId: sourceId,
      createdAt: '2026-07-01T12:00:00Z', lastMessageAt: '2026-07-24T15:00:00Z'
    }
    const source = chat.ensureSessionList(sourceId)
    source.loaded = true
    source.items = [session]
    const target = chat.ensureSessionList(targetId)
    target.loaded = true
    target.items = [
      { id: 'session-2', projectId: targetId, lastMessageAt: '2026-07-24T16:00:00Z' },
      { id: 'session-3', projectId: targetId, lastMessageAt: '2026-07-24T14:00:00Z' }
    ]
    chat.sessionCatalog = [...target.items, session]
    const { lastMessageAt, ...patchResult } = session
    api.patch.mockResolvedValue({ data: {
      ...patchResult, projectId: targetId, updatedAt: '2026-07-25T12:00:00Z'
    } })

    await chat.moveSession(session.id, targetId)

    expect(source.items).toEqual([])
    expect(target.items.map((item) => item.id)).toEqual(['session-2', 'session-1', 'session-3'])
    expect(target.items[1].lastMessageAt).toBe(lastMessageAt)
    expect(chat.sessionCatalog.map((item) => item.id)).toEqual(['session-2', 'session-1', 'session-3'])
    expect(chat.sessionCatalog[1].lastMessageAt).toBe(lastMessageAt)
  })

  it('keeps queued session file identity when a session moves into a project', async () => {
    api.patch.mockResolvedValue({
      data: { id: 'session-1', title: 'Session', projectId: 'project-2' }
    })
    const chat = useChatStore()
    chat.cancelQueueDrain()
    setSessions(chat, [{ id: 'session-1', title: 'Session', projectId: null }])
    chat.current = { id: 'session-1', title: 'Session', projectId: null, messages: [] }
    const queued = {
      id: 'queue-1',
      ownerUserId: 'user-1',
      sessionId: 'session-1',
      projectId: null,
      activeFile: {
        root: 'session',
        root_identity: 'session-1',
        path: 'note.txt',
        sha256: 'a'.repeat(64),
        dirty: false
      },
      queuedAt: '2026-07-20T12:00:00Z',
      attachments: []
    }
    await saveChatOutboxEntry(queued)
    chat.queuedMessages['session-1'] = [queued]

    await chat.moveSession('session-1', 'project-2')

    expect(chat.queuedMessages['session-1'][0].projectId).toBe('project-2')
    expect(chat.queuedMessages['session-1'][0].activeFile.root_identity).toBe('session-1')
    const persisted = await loadChatOutbox('user-1')
    expect(persisted[0].activeFile.root_identity).toBe('session-1')
  })

  it('does not let a stale destination refresh erase a moved session', async () => {
    let resolveRefresh
    api.get.mockReturnValueOnce(new Promise((resolve) => { resolveRefresh = resolve }))
      .mockResolvedValue(sessionPage([
        { id: 'session-1', title: 'Verschoben', projectId: 'project-2' },
        { id: 'session-2', title: 'Vorhanden', projectId: 'project-2' }
      ]))
    api.patch.mockResolvedValue({
      data: { id: 'session-1', title: 'Verschoben', projectId: 'project-2' }
    })
    const chat = useChatStore()
    const source = chat.ensureSessionList('project-1')
    source.loaded = true
    source.items = [{ id: 'session-1', title: 'Verschieben', projectId: 'project-1' }]
    const target = chat.ensureSessionList('project-2')
    target.loaded = true
    target.items = [{ id: 'session-2', title: 'Vorhanden', projectId: 'project-2' }]

    const refreshing = chat.refreshSessions(null, 'project-2')
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledOnce())
    await chat.moveSession('session-1', 'project-2')
    resolveRefresh(sessionPage([{ id: 'session-2', title: 'Alt', projectId: 'project-2' }]))
    await refreshing

    expect(target.items.map((session) => session.id)).toEqual(['session-1', 'session-2'])
  })

  it('pins a session and moves it ahead of unpinned sessions', async () => {
    api.patch.mockResolvedValue({
      data: {
        id: 'session-1',
        pinned: true,
        updatedAt: '2026-07-20T12:00:00Z'
      }
    })
    const chat = useChatStore()
    setSessions(chat, [
      { id: 'session-2', pinned: false, lastMessageAt: '2026-07-20T14:00:00Z' },
      { id: 'session-1', pinned: false, lastMessageAt: '2026-07-20T12:00:00Z' }
    ])
    chat.current = { id: 'session-1', pinned: false, messages: [] }

    await chat.setSessionPinned('session-1', true)

    expect(api.patch).toHaveBeenCalledWith('/sessions/session-1', { pinned: true })
    expect(chat.sessions.map((session) => session.id)).toEqual(['session-1', 'session-2'])
    expect(chat.current.pinned).toBe(true)
  })

  it('keeps the run active and reconnects after a transient SSE error', async () => {
    vi.useFakeTimers()
    global.EventSource = EventSourceMock
    api.get.mockResolvedValue({
      data: {
        id: 'run-1',
        status: 'running',
        error: null,
        createdAt: '2026-07-19T12:00:00Z'
      }
    })
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [] }
    chat.selectedSessionId = 'session-1'
    chat.watchRun('session-1', 'run-1', {
      status: 'running',
      createdAt: '2026-07-19T12:00:00Z'
    })

    const disconnected = EventSourceMock.instances[0]
    disconnected.emit('message.delta', { delta: 'Anfang' })
    await disconnected.onerror()

    expect(chat.running).toBe(true)
    expect(chat.activity.phase).toBe('reconnecting')
    await vi.advanceTimersByTimeAsync(1000)
    expect(EventSourceMock.instances).toHaveLength(2)
    expect(chat.running).toBe(true)
    disconnected.emit('message.delta', { delta: 'Veraltet' })
    const reconnected = EventSourceMock.instances[1]
    reconnected.emit('run.snapshot', { content: 'Anfang und Rest', events: [] })
    reconnected.emit('message.delta', { delta: '.' })
    expect(chat.streamContent).toBe('Anfang und Rest.')
  })

  it('releases the composer immediately while cancellation finishes', async () => {
    global.EventSource = EventSourceMock
    api.post.mockResolvedValue({ data: { status: 'cancelled' } })
    api.get.mockImplementation((url) => Promise.resolve({
      data: url === '/sessions/session-1'
        ? { id: 'session-1', messages: [], activeRun: null }
        : { items: [{ id: 'session-1', title: 'Test', activeRunStatus: null }], nextCursor: null }
    }))
    const chat = useChatStore()
    setSessions(chat, [{ id: 'session-1', title: 'Test' }])
    chat.current = { id: 'session-1', messages: [] }
    chat.selectedSessionId = 'session-1'
    chat.watchRun('session-1', 'run-1', { status: 'running' })

    await chat.stop()

    expect(api.post).toHaveBeenCalledWith('/sessions/session-1/runs/run-1/cancel')
    expect(chat.running).toBe(false)
    expect(chat.stopping).toBe(false)
    expect(chat.messageQueueActive).toBe(true)
    expect(chat.queuedSendReady).toBe(false)
    expect(chat.runId).toBe('run-1')

    EventSourceMock.instances[0].emit('run.cancelled', { runId: 'run-1' })

    expect(chat.running).toBe(false)
    expect(chat.stopping).toBe(false)
    expect(chat.runId).toBeNull()
    expect(chat.sessions[0].activeRunStatus).toBeNull()
  })

  it('promotes the last streamed state before a cancelled run finishes', () => {
    global.EventSource = EventSourceMock
    api.get.mockImplementation(() => new Promise(() => {}))
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [] }
    chat.selectedSessionId = 'session-1'
    chat.watchRun('session-1', 'run-1', { status: 'running' })
    const source = EventSourceMock.instances[0]

    source.emit('message.delta', { delta: 'Antwortende' })
    source.emit('reasoning.updated', {
      reasoning: { kind: 'content', text: 'Zwischenstand', status: 'streaming' }
    })
    source.emit('message.interrupted', {
      message: {
        id: 'message-1',
        role: 'assistant',
        content: 'Vollständige Teilantwort',
        generationStatus: 'interrupted',
        reasoning: { kind: 'content', text: 'Zwischenstand', status: 'interrupted' }
      }
    })
    source.emit('run.cancelled', { runId: 'run-1' })

    expect(chat.current.messages).toEqual([
      expect.objectContaining({
        id: 'message-1',
        content: 'Vollständige Teilantwort',
        generationStatus: 'interrupted',
        reasoning: expect.objectContaining({ text: 'Zwischenstand' })
      })
    ])
    expect(chat.streamContent).toBe('')
    expect(chat.streamReasoning).toBeNull()
    expect(chat.running).toBe(false)
  })

  it('keeps an empty interruption marker when no text was streamed', () => {
    global.EventSource = EventSourceMock
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [] }
    chat.selectedSessionId = 'session-1'
    chat.watchRun('session-1', 'run-1', { status: 'running' })

    EventSourceMock.instances[0].emit('message.interrupted', {
      message: {
        id: 'message-1',
        role: 'assistant',
        content: '',
        generationStatus: 'interrupted',
        reasoning: null
      }
    })

    expect(chat.current.messages).toEqual([
      expect.objectContaining({
        id: 'message-1',
        content: '',
        generationStatus: 'interrupted'
      })
    ])
  })

  it('queues an immediate stop until the run id is known', async () => {
    global.EventSource = EventSourceMock
    let resolveCreate
    api.post.mockImplementation((url) => {
      if (url === '/sessions') {
        return new Promise((resolve) => {
          resolveCreate = resolve
        })
      }
      return Promise.resolve({ data: { status: 'cancelled' } })
    })
    const chat = useChatStore()

    const sending = chat.send('Bitte stoppen')
    await chat.stop()
    expect(chat.running).toBe(false)
    expect(chat.stopping).toBe(false)

    await vi.waitFor(() => expect(resolveCreate).toBeTypeOf('function'))

    resolveCreate({
      data: {
        session: { id: 'session-1', messages: [] },
        runId: 'run-1'
      }
    })
    await sending

    expect(api.post).toHaveBeenCalledWith('/sessions/session-1/runs/run-1/cancel')
    expect(chat.running).toBe(false)
    expect(chat.stopping).toBe(false)
  })

  it('undoes a user message and restores its text as the session draft', async () => {
    const canonicalSession = {
      id: 'session-1',
      title: 'Test',
      messages: [{ id: 'user-1', role: 'user', content: 'Bleibt' }],
      activeRun: null
    }
    api.get.mockImplementation((url) => Promise.resolve({
      data: url === '/sessions/session-1' ? canonicalSession : []
    }))
    api.post.mockResolvedValue({
      data: {
        session: canonicalSession,
        draft: { content: 'Noch einmal bearbeiten' }
      }
    })
    const chat = useChatStore()
    setSessions(chat, [{ id: 'session-1', title: 'Test' }])
    chat.current = {
      id: 'session-1',
      title: 'Test',
      messages: [
        { id: 'user-1', role: 'user', content: 'Bleibt' },
        { id: 'user-2', role: 'user', content: 'Noch einmal bearbeiten', attachments: [] }
      ]
    }
    chat.selectedSessionId = 'session-1'
    chat.setDraft('Wird ersetzt')

    await chat.undoUserMessage(chat.current.messages[1])

    expect(api.post).toHaveBeenCalledWith(
      '/sessions/session-1/messages/user-2/undo',
      null,
      { suppressErrorToast: true }
    )
    expect(chat.current.messages).toEqual([{ id: 'user-1', role: 'user', content: 'Bleibt' }])
    expect(chat.activeDraft).toBe('Noch einmal bearbeiten')
  })

  it('undoes the first user message while its run is active', async () => {
    global.EventSource = EventSourceMock
    let resolveUndo
    api.get.mockResolvedValue({ data: { id: 'session-1', title: 'Neue Session', messages: [], activeRun: null } })
    api.post.mockImplementation((url) => url.endsWith('/undo')
      ? new Promise((resolve) => { resolveUndo = resolve })
      : Promise.resolve({ data: {} }))
    const chat = useChatStore()
    const message = { id: 'user-1', role: 'user', content: 'Noch einmal', attachments: [] }
    setSessions(chat, [{ id: 'session-1', title: 'Test', activeRunStatus: 'running' }])
    chat.current = { id: 'session-1', title: 'Test', messages: [message] }
    chat.selectedSessionId = 'session-1'
    chat.watchRun('session-1', 'run-1', { status: 'running' })

    const undoing = chat.undoUserMessage(message)
    await vi.waitFor(() => expect(resolveUndo).toBeTypeOf('function'))
    const source = EventSourceMock.instances[0]
    source.emit('message.interrupted', {
      message: {
        id: 'assistant-1',
        role: 'assistant',
        content: 'Teilantwort',
        generationStatus: 'interrupted'
      }
    })
    source.emit('run.cancelled', { runId: 'run-1' })

    expect(api.get.mock.calls.some(([url]) => url === '/sessions/session-1')).toBe(false)
    resolveUndo({ data: { draft: { content: 'Noch einmal' } } })
    await undoing

    expect(chat.current.id).toBe('session-1')
    expect(chat.current.messages).toEqual([])
    expect(chat.running).toBe(false)
    expect(chat.activeDraft).toBe('Noch einmal')
    expect(api.get.mock.calls.some(([url]) => url === '/sessions/session-1')).toBe(true)
  })

  it.each([
    [false, undefined], [true, undefined], [true, 'pending'], [true, 'failed'], [true, 'completed']
  ])('restores persisted attachments before undoing the message (voice: %s, status: %s)', async (voiceMessage, voiceTranscriptionStatus) => {
    const filename = voiceMessage ? 'aufnahme.wav' : 'notiz.txt'
    const mimeType = voiceMessage ? 'audio/wav' : 'text/plain'
    const sessionId = '11111111-1111-4111-8111-111111111111'
    const messageId = '22222222-2222-4222-8222-222222222222'
    const attachmentId = '33333333-3333-4333-8333-333333333333'
    const url = `/api/sessions/${sessionId}/messages/${messageId}/attachments/${attachmentId}`
    global.URL = { createObjectURL: vi.fn(() => 'blob:restored'), revokeObjectURL: vi.fn() }
    api.get.mockImplementation((requestUrl) => Promise.resolve({
      data: requestUrl === url
        ? new Blob(['inhalt'], { type: 'text/plain' })
        : requestUrl === `/sessions/${sessionId}`
          ? { id: sessionId, messages: [], activeRun: null }
          : []
    }))
    api.post.mockResolvedValue({
      data: {
        session: { id: sessionId, messages: [], activeRun: null },
        draft: { content: 'Mit Datei' }
      }
    })
    const chat = useChatStore()
    const message = {
      id: messageId,
      role: 'user',
      content: 'Mit Datei',
      attachments: [{ id: attachmentId, url, filename, mimeType, metadata: { voiceMessage, voiceTranscriptionStatus } }]
    }
    chat.current = { id: sessionId, messages: [message] }
    chat.selectedSessionId = sessionId

    await chat.undoUserMessage(message)

    expect(api.get).toHaveBeenCalledWith(url, {
      baseURL: '',
      responseType: 'blob',
      suppressErrorToast: true
    })
    expect(chat.activeAttachmentDraft).toHaveLength(1)
    expect(chat.activeAttachmentDraft[0].file.name).toBe(filename)
    expect(chat.activeAttachmentDraft[0].file.type).toBe(mimeType)
    expect(chat.activeAttachmentDraft[0].voiceMessage).toBe(voiceMessage && voiceTranscriptionStatus !== 'completed')
    const payload = JSON.parse(buildAttachmentFormData({ content: chat.activeDraft }, chat.activeAttachmentDraft).get('payload'))
    expect(payload.voiceAttachmentIndices).toEqual(voiceMessage && voiceTranscriptionStatus !== 'completed' ? [0] : [])
  })

  it('refreshes a transcribed user message without promoting or clearing the assistant stream', async () => {
    global.EventSource = EventSourceMock
    const chat = useChatStore()
    chat.current = { id: 'session-1', messages: [{ id: 'user-1', role: 'user', content: '' }] }
    chat.selectedSessionId = 'session-1'
    chat.watchRun('session-1', 'run-1')
    const source = EventSourceMock.instances[0]
    chat.streamContent = 'Unfinished assistant response'
    chat.streamReasoning = { text: 'Reasoning' }
    const phase = chat.activity.phase
    api.get.mockResolvedValue({ data: {
      id: 'session-1',
      messages: [{ id: 'user-1', role: 'user', content: 'Transcript', attachments: [{ metadata: { voiceTranscriptionStatus: 'completed' } }] }]
    } })

    source.emit('message.updated', { messageId: 'user-1' })
    await vi.waitFor(() => expect(chat.current.messages[0].content).toBe('Transcript'))
    expect(chat.current.messages).toHaveLength(1)
    expect(chat.current.messages[0].attachments[0].metadata.voiceTranscriptionStatus).toBe('completed')
    expect(chat.streamContent).toBe('Unfinished assistant response')
    expect(chat.streamReasoning).toEqual({ text: 'Reasoning' })
    expect(chat.activity.phase).toBe(phase)
    api.get.mockClear()
    chat.current = { id: 'session-2', messages: [] }
    chat.selectedSessionId = 'session-2'
    source.emit('message.updated', { messageId: 'user-1' })
    await Promise.resolve()
    expect(api.get).not.toHaveBeenCalled()
    chat.current = { id: 'session-1', messages: [] }
    chat.selectedSessionId = 'session-1'
    chat.eventSource = null
    source.emit('message.updated', { messageId: 'user-1' })
    await Promise.resolve()
    expect(api.get).not.toHaveBeenCalled()
  })

  it('keeps history and draft when undo fails', async () => {
    const error = new Error('request failed')
    error.response = { status: 409 }
    api.post.mockRejectedValue(error)
    const chat = useChatStore()
    const message = { id: 'user-1', role: 'user', content: 'Bleibt', attachments: [] }
    chat.current = { id: 'session-1', messages: [message] }
    chat.selectedSessionId = 'session-1'
    chat.setDraft('Vorhandener Entwurf')

    await expect(chat.undoUserMessage(message)).rejects.toThrow('request failed')

    expect(chat.current.messages).toEqual([message])
    expect(chat.activeDraft).toBe('Vorhandener Entwurf')
  })

  it('keeps an undone message as a draft after navigating away', async () => {
    let resolveUndo
    api.post.mockReturnValue(new Promise((resolve) => { resolveUndo = resolve }))
    api.get.mockResolvedValue(sessionPage())
    const chat = useChatStore()
    const message = { id: 'user-2', role: 'user', content: 'Später bearbeiten', attachments: [] }
    setSessions(chat, [{ id: 'session-1' }, { id: 'session-2' }])
    chat.current = {
      id: 'session-1',
      messages: [{ id: 'user-1', role: 'user', content: 'Bleibt' }, message]
    }
    chat.selectedSessionId = 'session-1'

    const undoing = chat.undoUserMessage(message)
    chat.current = { id: 'session-2', messages: [] }
    chat.selectedSessionId = 'session-2'
    resolveUndo({ data: { draft: { content: 'Später bearbeiten' } } })
    await undoing

    expect(chat.current.id).toBe('session-2')
    expect(chat.drafts.sessionMessages['session-1']).toBe('Später bearbeiten')
  })

  it('recovers an undo whose response was lost after the server commit', async () => {
    api.post.mockRejectedValue(new Error('connection lost'))
    const missing = new Error('not found')
    missing.response = { status: 404 }
    api.get
      .mockRejectedValueOnce(missing)
      .mockResolvedValueOnce({
        data: {
          id: 'session-1',
          messages: [{ id: 'user-1', role: 'user', content: 'Bleibt' }],
          activeRun: null
        }
      })
    const chat = useChatStore()
    const message = { id: 'user-2', role: 'user', content: 'Wiederherstellen', attachments: [] }
    chat.current = {
      id: 'session-1',
      messages: [{ id: 'user-1', role: 'user', content: 'Bleibt' }, message]
    }
    chat.selectedSessionId = 'session-1'

    const result = await chat.undoUserMessage(message)

    expect(result.draft.content).toBe('Wiederherstellen')
    expect(chat.current.messages).toEqual([{ id: 'user-1', role: 'user', content: 'Bleibt' }])
    expect(chat.activeDraft).toBe('Wiederherstellen')
  })

  it('retries the latest runtime error without replacing the user draft', async () => {
    global.EventSource = EventSourceMock
    api.get.mockResolvedValue(sessionPage())
    api.post.mockResolvedValue({
      data: {
        runId: 'run-2',
        modelName: 'Originalmodell',
        session: {
          id: 'session-1',
          messages: [{ id: 'user-1', role: 'user', content: 'Ursprünglicher Auftrag' }],
          activeRun: { id: 'run-2', status: 'queued', createdAt: '2026-07-25T14:32:00Z' }
        }
      }
    })
    const chat = useChatStore()
    const error = { id: 'error-1', role: 'assistant', kind: 'runtime_error' }
    chat.current = {
      id: 'session-1',
      messages: [{ id: 'user-1', role: 'user', content: 'Ursprünglicher Auftrag' }, error]
    }
    chat.selectedSessionId = 'session-1'
    chat.setDraft('Nicht verändern')

    await chat.retryFailedMessage(error)

    expect(api.post).toHaveBeenCalledWith(
      '/sessions/session-1/messages/error-1/retry',
      null,
      { suppressErrorToast: true }
    )
    expect(chat.current.messages).toEqual([
      { id: 'user-1', role: 'user', content: 'Ursprünglicher Auftrag' }
    ])
    expect(chat.activeDraft).toBe('Nicht verändern')
    expect(chat.runId).toBe('run-2')
    expect(chat.running).toBe(true)
  })
})
