import { createPinia, setActivePinia } from 'pinia'
import { Screen } from 'quasar'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { api } = vi.hoisted(() => ({ api: { patch: vi.fn(), put: vi.fn(), get: vi.fn() } }))
vi.mock('src/boot/axios', () => ({ api }))

import {
  DEFAULT_CONTROL_CENTER_SPLIT_RATIO,
  MAX_CONTROL_CENTER_SPLIT_RATIO,
  MIN_CONTROL_CENTER_SPLIT_RATIO,
  useControlCenterStore
} from '../src/stores/control-center'
import {
  browserControlCenterAvailable,
  controlCenterGlobalSectionIds,
  controlCenterIntegrationIds
} from 'src/app/control-center/manifest'
import { useChatStore } from 'src/features/chat/store'
import { useSessionTabsStore } from 'src/features/chat/session-tabs-store'

const originalLocalStorage = globalThis.localStorage
const originalMobile = Screen.lt.md
const testIntegrationIds = ['calendar', 'test-app']

function storage(initialValue = null) {
  let value = initialValue
  return {
    getItem: vi.fn(() => value),
    setItem: vi.fn((_key, nextValue) => { value = nextValue })
  }
}

function tab(appId) {
  return { tabId: appId, kind: 'app', appId, pinned: false }
}

function requestTab(appId) {
  return { tab_id: appId, kind: 'app', app_id: appId, pinned: false }
}

function response(tabs, activeAppId) {
  return {
    data: {
      controlCenter: {
        tabs: tabs.some((item) => item.appId === 'session') ? tabs : [tab('session'), ...tabs],
        activeAppId
      }
    }
  }
}

function requestState(appIds, activeAppId, fileView = null, fileViews = { session: null, filesystem: null }) {
  return {
    tabs: appIds.map(requestTab),
    active_app_id: activeAppId,
    file_view: fileView,
    file_views: fileViews
  }
}

const sessionFileView = {
  scope: 'session',
  selectedPath: 'drafts/draft-1/plan.md',
  selectedKind: 'file',
  viewedFilePath: 'drafts/draft-1/plan.md',
  rootIdentity: null,
  executionAgentId: null,
  currentDirectory: null
}

describe('control center store', () => {
  beforeEach(() => {
    delete globalThis.localStorage
    testIntegrationIds.forEach((id) => controlCenterIntegrationIds.add(id))
    setActivePinia(createPinia())
    vi.resetAllMocks()
    Screen.lt.md = false
  })

  afterEach(() => {
    Screen.lt.md = originalMobile
    testIntegrationIds.forEach((id) => controlCenterIntegrationIds.delete(id))
    if (originalLocalStorage) globalThis.localStorage = originalLocalStorage
    else delete globalThis.localStorage
  })

  it('starts drafts with the fixed Session app and files integration', () => {
    const store = useControlCenterStore()
    expect(store.tabs).toEqual([tab('session'), tab('files')])
    expect(store.activeAppId).toBe('session')
    expect(store.activeContentId).toBe('session')
    expect(store.open).toBe(false)
    expect(store.fileView).toBeNull()
  })

  it('opens Git settings on a draft without creating a session or worktree', async () => {
    const create = vi.spyOn(useChatStore(), 'createEmptySession')
    await useControlCenterStore().openApp('git')
    expect(create).not.toHaveBeenCalled()
  })

  it('keeps global sections outside the integration manifest', () => {
    expect([...controlCenterGlobalSectionIds]).toEqual(['notifications'])
    expect(controlCenterIntegrationIds.has('agent-coding')).toBe(true)
    expect(controlCenterIntegrationIds.has('notifications')).toBe(false)
    expect(controlCenterIntegrationIds.has('approvals')).toBe(false)
  })

  it('offers a configured browser with the draft computer before a session exists', () => {
    const draft = { preferredExecutionAgentId: 'agent-1' }
    const agents = { byId: () => ({ connected: true }) }
    const integrations = {
      adapterAvailable: () => true,
      browser: { instances: [{ id: 'browser-1', enabled: true, executionAgentId: 'agent-1' }] }
    }
    expect(browserControlCenterAvailable(draft, agents, integrations)).toBe(true)
    expect(browserControlCenterAvailable({ ...draft, preferredExecutionAgentId: null }, agents, integrations)).toBe(false)
  })

  it('loads and persists the existing validated split ratio', () => {
    globalThis.localStorage = storage('64')
    const store = useControlCenterStore()

    expect(store.splitRatio).toBe(64)
    store.setSplitRatio(67)
    store.setSplitRatio(90)

    expect(store.splitRatio).toBe(67)
    expect(MIN_CONTROL_CENTER_SPLIT_RATIO).toBe(30)
    expect(MAX_CONTROL_CENTER_SPLIT_RATIO).toBe(70)
    expect(globalThis.localStorage.setItem).toHaveBeenCalledWith('matoLoopWorkspaceSplitRatio', '67')
  })

  it('falls back when the persisted split ratio is invalid', () => {
    globalThis.localStorage = storage('invalid')
    expect(useControlCenterStore().splitRatio).toBe(DEFAULT_CONTROL_CENTER_SPLIT_RATIO)
  })

  it('keeps global visibility across session binding, synchronization and draft resets', async () => {
    const store = useControlCenterStore()
    const workspace = useSessionTabsStore()
    workspace.$patch({ owner: 'user-1', loaded: true })
    api.put.mockResolvedValue({ data: { sessionTabs: [], controlCenterOpen: true } })
    const session = { id: 'session-1', controlCenter: { tabs: [tab('files')], activeAppId: 'files', open: true } }
    store.bindSession(session)
    store.syncSession(session)
    expect(store.open).toBe(false)

    await store.setOpen(true)
    expect(api.put).toHaveBeenCalledWith('/users/me/workspace-state', { controlCenterOpen: true })
    const nextSession = { id: 'session-2', controlCenter: { tabs: [tab('calendar')], activeAppId: 'calendar', open: false } }
    store.bindSession(nextSession)
    expect(store.open).toBe(true)
    store.syncSession(nextSession)
    expect(store.open).toBe(true)
    store.resetDraft()
    expect(store.open).toBe(true)

    api.put.mockResolvedValue({ data: { sessionTabs: [], controlCenterOpen: false } })
    await store.setOpen(false)
    store.bindSession(session)
    store.syncSession(session)
    store.resetDraft()
    expect(store.open).toBe(false)
    expect(workspace.controlCenterOpen).toBe(false)
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('restores visibility after a failed workspace save', async () => {
    const store = useControlCenterStore()
    const workspace = useSessionTabsStore()
    workspace.$patch({ owner: 'user-1', loaded: true })
    api.put.mockRejectedValueOnce(new Error('request failed'))

    await expect(store.setOpen(true)).rejects.toThrow('request failed')

    expect(store.open).toBe(false)
    expect(workspace.controlCenterOpen).toBe(false)
  })

  it('changes mobile visibility without writing the desktop preference', async () => {
    Screen.lt.md = true
    const store = useControlCenterStore()
    const workspace = useSessionTabsStore()
    workspace.$patch({ owner: 'user-1', loaded: true })

    await store.setOpen(true)

    expect(store.open).toBe(true)
    expect(workspace.controlCenterOpen).toBe(false)
    expect(api.put).not.toHaveBeenCalled()
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('does not reopen after an explicit close while app state is saving', async () => {
    Screen.lt.md = true
    const store = useControlCenterStore()
    store.syncSession({ id: 'session-1', controlCenter: { tabs: [tab('files')], activeAppId: 'files' } })
    let resolve
    api.patch.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const opening = store.openApp('session')
    expect(store.open).toBe(true)
    await store.setOpen(false)
    resolve(response([tab('files')], 'session'))
    await opening
    expect(store.open).toBe(false)
  })

  it('does not reopen or persist a preview closed while loading the desktop preference', async () => {
    const store = useControlCenterStore()
    useSessionTabsStore().$patch({ owner: 'user-1' })
    let finishWorkspace
    api.get.mockReturnValueOnce(new Promise((resolve) => { finishWorkspace = resolve }))

    const opening = store.openApp('session')
    expect(store.open).toBe(true)
    const closing = store.setOpen(false)
    finishWorkspace({ data: { sessionTabs: [], controlCenterOpen: false } })
    await Promise.all([opening, closing])

    expect(store.open).toBe(false)
    expect(api.put).not.toHaveBeenCalled()
  })

  it('reopens the confirmed active app without patching the session', async () => {
    const store = useControlCenterStore()
    store.syncSession({ id: 'session-1', controlCenter: { tabs: [tab('files')], activeAppId: 'session' } })
    await store.openApp('session')
    expect(api.patch).not.toHaveBeenCalled()
  })

  it.each(['notifications', 'approvals'])('opens %s as notifications without saving session state', async (appId) => {
    const store = useControlCenterStore()
    store.syncSession({ id: 'session-1', controlCenter: { tabs: [tab('files')], activeAppId: 'files' } })

    await store.openApp(appId)
    expect(store.open).toBe(true)
    expect(store.activeGlobalSectionId).toBe('notifications')
    expect(store.activeContentId).toBe('notifications')
    expect(store.tabs).toEqual([tab('session'), tab('files')])
    expect(api.patch).not.toHaveBeenCalled()

    store.syncSession({ id: 'session-2', controlCenter: { tabs: [tab('calendar')], activeAppId: 'calendar' } })
    expect(store.activeContentId).toBe('calendar')
    await store.activate('calendar')
    expect(store.activeGlobalSectionId).toBeNull()
    expect(store.activeContentId).toBe('calendar')
  })

  it('normalizes legacy global and pinned tabs to ordered integrations', () => {
    const store = useControlCenterStore()
    store.syncSession({
      id: 'session-1',
      controlCenter: {
        tabs: [
          tab('files'),
          tab('notifications'),
          { appId: 'test-app', pinned: true },
          tab('approvals'),
          tab('calendar')
        ],
        activeAppId: 'notifications'
      }
    })

    expect(store.tabs).toEqual([tab('session'), tab('test-app'), tab('files'), tab('calendar')])
    expect(store.activeAppId).toBe('session')
  })

  it('adds and persists an integration for the active session', async () => {
    let finishSave
    api.patch.mockReturnValueOnce(new Promise((resolve) => { finishSave = resolve }))
    api.put.mockResolvedValue({ data: { sessionTabs: [], controlCenterOpen: true } })
    const store = useControlCenterStore()
    useSessionTabsStore().$patch({ owner: 'user-1', loaded: true })
    store.syncSession({ id: 'session-1', controlCenter: { tabs: [tab('files')], activeAppId: 'files' } })

    const opening = store.openApp('calendar')
    expect(store.open).toBe(true)
    expect(api.put).not.toHaveBeenCalled()
    finishSave(response([tab('files'), tab('calendar')], 'calendar'))
    await opening

    expect(store.activeContentId).toBe('calendar')
    expect(store.open).toBe(true)
    expect(api.patch).toHaveBeenCalledWith('/sessions/session-1', {
      control_center: requestState(['session', 'files', 'calendar'], 'calendar')
    })
    expect(api.put).toHaveBeenCalledWith('/users/me/workspace-state', { controlCenterOpen: true })
  })

  it('adds a background integration without opening or activating the panel', async () => {
    api.patch.mockResolvedValue(response([tab('files'), tab('browser')], 'files'))
    const store = useControlCenterStore()
    store.syncSession({ id: 'session-1', controlCenter: { tabs: [tab('files')], activeAppId: 'files' } })

    await store.ensureApp('browser')
    await store.ensureApp('browser')

    expect(store.open).toBe(false)
    expect(store.activeAppId).toBe('files')
    expect(api.patch).toHaveBeenCalledOnce()
    expect(api.patch).toHaveBeenCalledWith('/sessions/session-1', {
      control_center: requestState(['session', 'files', 'browser'], 'files'),
      browser_tab_action: 'add'
    })
  })

  it('keeps an explicit Browser removal when a later layout write is queued', async () => {
    let finishSave
    api.patch.mockReturnValueOnce(new Promise(resolve => { finishSave = resolve }))
      .mockResolvedValueOnce(response([tab('files')], 'session'))
    const store = useControlCenterStore()
    store.syncSession({
      id: 'session-1', controlCenter: { tabs: [tab('files'), tab('browser')], activeAppId: 'session' }
    })

    const saving = store.activate('files')
    await store.removeIntegration('browser')
    await store.activate('session')
    finishSave(response([tab('files'), tab('browser')], 'files'))
    await saving

    expect(api.patch).toHaveBeenLastCalledWith('/sessions/session-1', {
      control_center: requestState(['session', 'files'], 'session'),
      browser_tab_action: 'remove'
    })
  })

  it('reorders and removes integrations explicitly', async () => {
    api.patch
      .mockResolvedValueOnce(response([tab('calendar'), tab('files'), tab('test-app')], 'files'))
      .mockResolvedValueOnce(response([tab('calendar'), tab('test-app')], 'calendar'))
    const store = useControlCenterStore()
    store.syncSession({
      id: 'session-1',
      controlCenter: { tabs: [tab('files'), tab('calendar'), tab('test-app')], activeAppId: 'files' }
    })

    await store.moveIntegration('files', 1)
    expect(store.tabs.map((item) => item.appId)).toEqual(['session', 'calendar', 'files', 'test-app'])

    await store.removeIntegration('files')
    expect(store.tabs.map((item) => item.appId)).toEqual(['session', 'calendar', 'test-app'])
    expect(store.activeAppId).toBe('calendar')
  })

  it('restores the prior server state after a failed mutation', async () => {
    api.patch.mockRejectedValue(new Error('request failed'))
    const store = useControlCenterStore()
    useSessionTabsStore().$patch({ owner: 'user-1', loaded: true })
    store.syncSession({ id: 'session-1', controlCenter: { tabs: [tab('files')], activeAppId: 'files' } })

    await expect(store.openApp('calendar')).rejects.toThrow('request failed')

    expect(store.tabs).toEqual([tab('session'), tab('files')])
    expect(store.activeAppId).toBe('files')
    expect(store.open).toBe(false)
    expect(api.put).not.toHaveBeenCalled()
  })

  it('unlocks persistence when the session changes during a save', async () => {
    let resolveFirst
    api.patch
      .mockReturnValueOnce(new Promise((resolve) => { resolveFirst = resolve }))
      .mockResolvedValueOnce(response([tab('files'), tab('test-app')], 'files'))
    const store = useControlCenterStore()
    store.syncSession({ id: 'session-1', controlCenter: { tabs: [tab('files')], activeAppId: 'files' } })
    const oldSave = store.openApp('calendar')

    store.syncSession({ id: 'session-2', controlCenter: { tabs: [tab('files')], activeAppId: 'files' } })
    expect(store.saving).toBe(false)
    await store.ensureApp('test-app')
    resolveFirst(response([tab('files'), tab('calendar')], 'calendar'))
    await oldSave

    expect(store.sessionId).toBe('session-2')
    expect(store.tabs).toEqual([tab('session'), tab('files'), tab('test-app')])
    expect(store.open).toBe(false)
    expect(store.saving).toBe(false)
  })

  it('persists the latest overlapping integration state', async () => {
    let resolveFirst
    api.patch
      .mockReturnValueOnce(new Promise((resolve) => { resolveFirst = resolve }))
      .mockResolvedValueOnce(response([tab('files'), tab('calendar'), tab('test-app')], 'test-app'))
    const store = useControlCenterStore()
    store.syncSession({ id: 'session-1', controlCenter: { tabs: [tab('files')], activeAppId: 'files' } })

    const first = store.openApp('calendar')
    await store.openApp('test-app')
    resolveFirst(response([tab('files'), tab('calendar')], 'calendar'))
    await first

    expect(api.patch).toHaveBeenCalledTimes(2)
    expect(store.activeAppId).toBe('test-app')
    expect(store.tabs.map((item) => item.appId)).toEqual(['session', 'files', 'calendar', 'test-app'])
  })

  it('persists draft integration changes made after session creation starts', async () => {
    const store = useControlCenterStore()
    await store.save({ tabs: [...store.tabs, tab('calendar')], activeAppId: 'calendar', open: true })
    api.patch.mockResolvedValue(response([tab('files'), tab('calendar')], 'calendar'))

    const state = await store.attachCreatedSession('session-1', {
      tabs: [tab('files')],
      activeAppId: 'files'
    })

    expect(api.patch).toHaveBeenCalledWith('/sessions/session-1', {
      control_center: requestState(['session', 'files', 'calendar'], 'calendar')
    })
    expect(state.activeAppId).toBe('calendar')
  })

  it('restores and persists the open file view as session state', async () => {
    const store = useControlCenterStore()
    store.syncSession({
      id: 'session-1',
      controlCenter: {
        tabs: [tab('files')],
        activeAppId: 'files',
        open: true,
        fileView: sessionFileView
      }
    })
    api.patch.mockResolvedValue({
      data: {
        controlCenter: {
          tabs: [tab('files')],
          activeAppId: 'files',
          open: false,
          fileView: sessionFileView
        }
      }
    })

    await store.setFileView(sessionFileView)

    expect(store.open).toBe(false)
    expect(store.fileView).toEqual(sessionFileView)
    expect(api.patch).toHaveBeenCalledWith('/sessions/session-1', {
      control_center: requestState(
        ['session', 'files'],
        'files',
        {
          scope: 'session',
          selected_path: sessionFileView.selectedPath,
          selected_kind: 'file',
          viewed_file_path: sessionFileView.viewedFilePath,
          root_identity: null,
          execution_agent_id: null,
          current_directory: null
        },
        {
          session: {
            scope: 'session',
            selected_path: sessionFileView.selectedPath,
            selected_kind: 'file',
            viewed_file_path: sessionFileView.viewedFilePath,
            root_identity: null,
            execution_agent_id: null,
            current_directory: null
          },
          filesystem: null
        }
      )
    })
  })

  it('opens each file once as a persistent resource tab', async () => {
    const store = useControlCenterStore()
    const target = {
      scope: 'session',
      path: 'notes/plan.md',
      name: 'plan.md',
      rootIdentity: '550e8400-e29b-41d4-a716-446655440000',
      readOnly: false
    }

    const tabId = await store.openFile(target)
    expect(tabId).toMatch(/^file-/u)
    expect(store.activeAppId).toBe(tabId)
    expect(store.fileTabs).toEqual([expect.objectContaining({
      tabId,
      kind: 'file',
      appId: 'file-viewer',
      fileTarget: target
    })])

    await expect(store.openFile(target)).resolves.toBe(tabId)
    expect(store.fileTabs).toHaveLength(1)

    await store.setOpen(false)
    await store.openApp(tabId)
    expect(store.open).toBe(true)
    expect(store.activeAppId).toBe(tabId)

    await store.rebindFileTargets(target, 'notes/final.md')
    expect(store.fileTabs[0].fileTarget).toEqual({
      ...target,
      path: 'notes/final.md',
      name: 'final.md'
    })
    await store.rebindFileTargets(store.fileTabs[0].fileTarget)
    expect(store.fileTabs).toEqual([])
    expect(store.activeAppId).toBe('files')
  })

  it('returns to each file explorer rather than a neighboring file tab', async () => {
    const store = useControlCenterStore()
    await store.ensureApp('computer-files')
    const stored = await store.openFile({
      scope: 'session', path: 'notes.md', rootIdentity: '550e8400-e29b-41d4-a716-446655440000'
    }, { returnTabId: 'files' })
    const local = await store.openFile({
      scope: 'filesystem', path: '/repo/local.md', executionAgentId: 'agent-1', currentDirectory: '/repo'
    }, { returnTabId: 'computer-files' })

    await store.closeFile(local)
    const afterLocal = store.activeAppId
    await store.openApp(stored)
    await store.closeFile(stored)
    expect([afterLocal, store.activeAppId]).toEqual(['computer-files', 'files'])
  })

  it('reveals a legacy read-only computer tab as an editable file from session state', async () => {
    const store = useControlCenterStore()
    const tabId = 'file-550e8400-e29b-41d4-a716-446655440000'
    const fileTarget = {
      scope: 'filesystem', executionAgentId: 'agent-1', currentDirectory: '/project',
      path: '/project/notes.md', name: 'notes.md', readOnly: true
    }
    const state = { tabs: [{ tabId, kind: 'file', fileTarget }], activeAppId: tabId, open: true }
    store.syncSession({ id: 'session-1', controlCenter: state })

    await expect(store.openFile({ ...fileTarget, readOnly: false })).resolves.toBe(tabId)
    expect(store.activeTab.fileTarget.readOnly).toBe(false)
    expect(store.open).toBe(true)
    expect(api.patch).not.toHaveBeenCalled()
  })

  it.each(['removeFileTabs', 'rebindFileTargets'])('sets comparison mode before activation and clears it through %s', async (removal) => {
    const store = useControlCenterStore()
    const opening = store.openFile({
      scope: 'filesystem', executionAgentId: 'agent-1', currentDirectory: '/repo',
      path: '/repo/deleted.txt', name: 'deleted.txt'
    }, { diff: true })
    expect(store.fileDiffModes[store.activeAppId]).toBe(true)
    await opening
    const tabId = store.activeAppId
    if (removal === 'removeFileTabs') await store.removeFileTabs([tabId])
    else await store.rebindFileTargets(store.activeTab.fileTarget)
    expect(store.fileDiffModes[tabId]).toBeUndefined()
  })

  it('keys historical tabs by root, file identity and revision independently of the current file', async () => {
    const store = useControlCenterStore()
    const target = {
      scope: 'session', path: 'notes.md', name: 'notes.md',
      rootIdentity: '550e8400-e29b-41d4-a716-446655440000',
      fileId: '550e8400-e29b-41d4-a716-446655440001', revision: 1
    }
    const historical = await store.openFile(target)
    expect(store.activeTab.fileTarget.readOnly).toBe(true)
    expect(await store.openFile({ ...target, path: 'renamed.md' })).toBe(historical)
    expect(await store.openFile({ ...target, revision: 2 })).not.toBe(historical)
    expect(await store.openFile({ ...target, revision: null })).not.toBe(historical)
    expect(await store.openFile({ ...target, rootIdentity: '550e8400-e29b-41d4-a716-446655440002' })).not.toBe(historical)
    expect(store.fileTabs).toHaveLength(4)
    await store.rebindFileTargets(target, 'renamed.md')
    expect(store.fileTabs.find((tab) => tab.tabId === historical).fileTarget).toMatchObject({
      path: 'renamed.md', fileId: target.fileId, revision: 1, readOnly: true
    })
    await store.rebindFileTargets({ ...target, path: 'renamed.md' })
    expect(store.fileTabs).toHaveLength(1)
    expect(store.fileTabs[0].fileTarget.rootIdentity).not.toBe(target.rootIdentity)
  })

  it('defers integration shortcuts until authoritative session state is loaded', async () => {
    const store = useControlCenterStore()
    useSessionTabsStore().$patch({ owner: 'user-1', loaded: true })
    api.put.mockResolvedValue({ data: { sessionTabs: [], controlCenterOpen: true } })
    store.bindSession({
      id: 'session-1',
      controlCenter: { tabs: [tab('files')], activeAppId: 'files' }
    })

    await store.openApp('calendar')
    expect(api.patch).not.toHaveBeenCalled()
    expect(api.put).not.toHaveBeenCalled()
    expect(store.requestedAppId).toBe('calendar')
    api.patch.mockResolvedValue(response([tab('files'), tab('calendar')], 'calendar'))
    store.syncSession({
      id: 'session-1',
      controlCenter: { tabs: [tab('files')], activeAppId: 'files' }
    })
    await vi.waitFor(() => expect(api.put).toHaveBeenCalledWith('/users/me/workspace-state', { controlCenterOpen: true }))

    expect(api.patch.mock.calls[0][1].control_center.tabs).toEqual([
      requestTab('session'),
      requestTab('files'),
      requestTab('calendar')
    ])
  })

  it('closes the local preview without persisting visibility when a deferred app save fails', async () => {
    const store = useControlCenterStore()
    useSessionTabsStore().$patch({ owner: 'user-1', loaded: true })
    const session = { id: 'session-1', controlCenter: { tabs: [tab('files')], activeAppId: 'files' } }
    store.bindSession(session)
    await store.openApp('calendar')
    const previewWasVisible = store.open
    api.patch.mockRejectedValueOnce(new Error('request failed'))
    store.syncSession(session)
    await vi.waitFor(() => expect(store.saving).toBe(false))
    expect([previewWasVisible, store.activeAppId, store.open, api.put.mock.calls.length]).toEqual([true, 'files', false, 0])
  })

  it.each(['calendar', 'notifications', 'approvals'])('does not carry a deferred %s shortcut into another session', async (appId) => {
    const store = useControlCenterStore()
    store.bindSession({
      id: 'session-1',
      controlCenter: { tabs: [tab('files')], activeAppId: 'files' }
    })
    await store.openApp(appId)

    store.bindSession({
      id: 'session-2',
      controlCenter: { tabs: [tab('files')], activeAppId: 'files' }
    })
    store.syncSession({
      id: 'session-2',
      controlCenter: { tabs: [tab('files')], activeAppId: 'files' }
    })

    expect(store.requestedAppId).toBeNull()
    expect(store.activeGlobalSectionId).toBeNull()
    expect(store.activeAppId).toBe('files')
    expect(api.patch).not.toHaveBeenCalled()
  })

  it.each(['notifications', 'approvals'])('keeps %s open when authoritative session state is loaded', async (appId) => {
    const store = useControlCenterStore()
    store.bindSession({
      id: 'session-1',
      controlCenter: { tabs: [tab('files')], activeAppId: 'files' }
    })

    await store.openApp(appId)

    expect(store.activeGlobalSectionId).toBe('notifications')
    expect(store.open).toBe(true)
    expect(store.requestedAppId).toBe('notifications')
    expect(api.patch).not.toHaveBeenCalled()

    store.syncSession({
      id: 'session-1',
      controlCenter: { tabs: [tab('files')], activeAppId: 'files', open: false }
    })

    expect(store.activeContentId).toBe('notifications')
    expect(store.open).toBe(true)
    expect(store.requestedAppId).toBeNull()
    expect(api.patch).not.toHaveBeenCalled()
  })
})
