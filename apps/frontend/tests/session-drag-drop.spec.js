// @vitest-environment jsdom
import { createApp, h, nextTick, reactive } from 'vue'
import { createPinia } from 'pinia'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import MainLayout from '../src/app/MainLayout.vue'
import { useChatStore } from 'src/features/chat/store'
import { useBrowserStore } from 'stores/browser'
import { useControlCenterStore } from 'stores/control-center'
import { useIntegrationStore } from 'stores/integrations'
import { useAutomationStore } from 'stores/automations'
import { useInAppNotificationStore } from 'stores/in-app-notifications'
import { useRoute } from 'vue-router'

const { chat, confirm, notifications, projects, pending, quasar, routeUpdate, terminal, workspace } = vi.hoisted(() => {
  const sessions = [
    { id: 'standalone', title: 'Standalone', projectId: null },
    { id: 'project-session', title: 'Project session', projectId: 'a' }
  ]
  const sessionLists = Object.fromEntries([null, 'a', 'b'].map((id) => [id ?? 'standalone', {
    loaded: true, items: sessions.filter((session) => session.projectId === id)
  }]))
  return {
    terminal: { syncContext: vi.fn(), release: vi.fn(), close: vi.fn() },
    workspace: {
      hasDirtyEditors: false, sessionId: null,
      fileTabDirty: vi.fn(() => false), discardFileTab: vi.fn()
    },
    confirm: vi.fn(),
    notifications: {
      unreadCount: 0,
      fetchLatest: vi.fn().mockResolvedValue(),
      markPresent: vi.fn().mockResolvedValue(),
      reconcilePush: vi.fn().mockResolvedValue()
    },
    routeUpdate: { callback: null },
    chat: {
      cachedSessions: sessions, sessionLists,
      sessionList(id) { return this.sessionLists[id ?? 'standalone'] },
      ensureSessionList(id) { return this.sessionLists[id ?? 'standalone'] },
      setSessionScope: vi.fn().mockResolvedValue(),
      loadSession: vi.fn().mockResolvedValue(),
      moveSession: vi.fn().mockResolvedValue(),
      setSessionPinned: vi.fn().mockResolvedValue(),
      findCachedSession: vi.fn((id) => sessions.find((session) => session.id === id)),
      fetchSession: vi.fn(),
      refreshSession: vi.fn().mockResolvedValue(),
      refreshSessions: vi.fn().mockResolvedValue(),
      markSessionRead: vi.fn().mockResolvedValue()
    },
    projects: { loaded: true, items: [{ id: 'a', name: 'Alpha' }, { id: 'b', name: 'Beta' }], fetchAll: vi.fn().mockResolvedValue() },
    pending: { pending: [], forSession: () => [], fetchPending: vi.fn().mockResolvedValue(), fetchGrants: vi.fn().mockResolvedValue() },
    quasar: { dark: { isActive: false }, screen: { lt: { sm: false, md: false, lg: false } }, platform: { is: {} }, notify: vi.fn() }
  }
})
vi.mock('quasar', () => ({ useQuasar: () => quasar }))
vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal()
  const { reactive } = await import('vue')
  const route = reactive({ name: 'chat', query: {} })
  const router = { push: vi.fn(async (to) => {
    const next = { name: to.name || route.name, query: to.query || {} }
    if (await routeUpdate.callback?.(next, { name: route.name, query: route.query }) === false) return { type: actual.NavigationFailureType.aborted }
    route.name = next.name
    route.query = next.query
  }) }
  router.replace = router.push
  return {
    ...actual,
    useRoute: () => route,
    useRouter: () => router,
    onBeforeRouteLeave: vi.fn(),
    onBeforeRouteUpdate: (callback) => { routeUpdate.callback = callback }
  }
})
vi.mock('src/features/chat/store', async () => {
  const { reactive } = await import('vue')
  const store = reactive(chat)
  return { useChatStore: () => store }
})
vi.mock('stores/projects', () => ({ useProjectStore: () => projects }))
vi.mock('src/features/home/store', () => ({ useHomeStore: () => ({ load: vi.fn().mockResolvedValue() }) }))
vi.mock('stores/approvals', () => ({ useApprovalStore: () => pending }))
vi.mock('stores/questions', () => ({ useQuestionStore: () => pending }))
vi.mock('stores/agent-identity', () => ({ useAgentIdentityStore: () => ({ fetchIdentity: vi.fn().mockResolvedValue() }) }))
vi.mock('stores/in-app-notifications', async () => {
  const { reactive } = await import('vue')
  const store = reactive(notifications)
  return { useInAppNotificationStore: () => store }
})
vi.mock('stores/control-center', async () => {
  const { reactive } = await import('vue')
  const store = reactive({
    sessionId: null, ready: true, open: false, restoredOpen: false, tabs: [], fileTabs: [], confirmedState: { tabs: [] },
    openApp: vi.fn(async function () { this.open = true; this.restoredOpen = false }),
    ensureApp: vi.fn().mockResolvedValue(),
    removeFileTabs: vi.fn().mockResolvedValue()
  })
  return { useControlCenterStore: () => store }
})
vi.mock('stores/terminal', () => ({ useTerminalStore: () => terminal }))
vi.mock('stores/browser', async () => {
  const { reactive } = await import('vue')
  const store = reactive({ sessionId: null, agentId: null, integrationId: null,
    syncContext: vi.fn(), release: vi.fn(), setDisplayedSession: vi.fn() })
  return { useBrowserStore: () => store }
})
vi.mock('stores/integrations', async () => {
  const { reactive } = await import('vue')
  const store = reactive({ fetchCatalog: vi.fn().mockResolvedValue(), fetchBrowser: vi.fn().mockResolvedValue(), fetchTbsDesk: vi.fn().mockResolvedValue(),
    adapterAvailable: vi.fn(() => true), browser: { instances: [] }, browserLoaded: true, catalogLoaded: true, catalogLoadFailed: false })
  return { useIntegrationStore: () => store }
})
vi.mock('stores/auth', () => ({ useAuthStore: () => ({ username: 'Tester' }) }))
vi.mock('stores/automations', async () => {
  const { reactive } = await import('vue')
  const store = reactive({ items: [], fetchAutomations: vi.fn().mockResolvedValue(), deleteAutomation: vi.fn().mockResolvedValue() })
  return { useAutomationStore: () => store }
})
vi.mock('stores/execution-agents', () => ({ useExecutionAgentStore: () => ({ byId: (id) => id === 'agent-1' ? { connected: true, operations: [] } : null }) }))
vi.mock('stores/workspace', () => ({ useWorkspaceStore: () => workspace }))
vi.mock('stores/search', () => ({ useSearchStore: () => ({ openDialog: vi.fn() }) }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm }) }))
vi.mock('src/utils/notifications', () => ({ notifyError: vi.fn() }))
vi.mock('src/features/auth/AccountDialog.vue', () => ({ default: { render: () => null } }))
vi.mock('src/features/search/SearchDialog.vue', () => ({ default: { render: () => null } }))
vi.mock('src/app/control-center/ControlCenterPanel.vue', () => ({ default: { render: () => null } }))
vi.mock('src/features/computers/ExecutionAgentDirectoryPickerDialog.vue', () => ({ default: { render: () => null } }))

let app, root
beforeEach(async () => {
  vi.clearAllMocks()
  useInAppNotificationStore().unreadCount = 0
  document.head.innerHTML = '<title>MatoLoop</title><link rel="icon" type="image/svg+xml" href="/icons/matoloop.svg">'
  useAutomationStore().items = []
  quasar.dark.isActive = false
  quasar.screen = reactive({ lt: { sm: false, md: false, lg: false } })
  useChatStore().current = null
  useRoute().name = 'chat'
  useRoute().query = {}
  Object.assign(useBrowserStore(), { sessionId: null, agentId: null, integrationId: null })
  Object.assign(useControlCenterStore(), { sessionId: null, ready: true, open: false, restoredOpen: false, tabs: [], fileTabs: [], confirmedState: { tabs: [] } })
  useIntegrationStore().browser.instances = []
  Object.assign(useIntegrationStore(), { browserLoaded: true, catalogLoaded: true, catalogLoadFailed: false })
  useIntegrationStore().adapterAvailable.mockReturnValue(true)
  workspace.hasDirtyEditors = false
  workspace.sessionId = null
  workspace.fileTabDirty.mockReturnValue(false)
  vi.useFakeTimers()
  vi.stubGlobal('EventSource', class {
    static instances = []
    constructor(url) {
      this.url = url
      this.listeners = {}
      this.close = vi.fn()
      this.constructor.instances.push(this)
    }
    addEventListener(name, callback) { this.listeners[name] = callback }
    emit(changes) { this.listeners.changed({ data: JSON.stringify(changes) }) }
  })
  root = document.createElement('div')
  app = createApp(MainLayout)
  app.use(createPinia())
  app.config.globalProperties.$q = quasar
  const container = { render() { return h('div', this.$slots.default?.()) } }
  for (const name of ['QLayout', 'QDrawer', 'QToolbar', 'QSpace', 'QSeparator', 'QScrollArea', 'QItem', 'QItemSection', 'QItemLabel', 'QIcon', 'QList', 'QAvatar', 'QBadge', 'QCard', 'QCardSection', 'QCardActions', 'QBanner', 'QPageContainer', 'RouterLink']) {
    app.component(name, container)
  }
  for (const name of ['QTooltip', 'QInput', 'QSkeleton', 'QSpinner', 'QSpinnerDots', 'QSpinnerGrid', 'RouterView']) app.component(name, { render: () => null })
  app.component('QMenu', {
    props: ['modelValue'], emits: ['update:modelValue'],
    render() {
      return h('span', { class: 'menu-stub' }, [
        h('button', { class: 'menu-stub__show', onClick: () => this.$emit('update:modelValue', true) }),
        h('button', { class: 'menu-stub__hide', onClick: () => this.$emit('update:modelValue', false) }),
        this.modelValue ? this.$slots.default?.() : null
      ])
    }
  })
  app.component('QExpansionItem', {
    props: ['modelValue'], emits: ['update:modelValue', 'show'],
    render() {
      return h('div', [h('button', { onClick: () => {
        this.$emit('update:modelValue', !this.modelValue)
        if (!this.modelValue) this.$emit('show')
      } }, this.$slots.header?.()), this.modelValue ? this.$slots.default?.() : null])
    }
  })
  app.component('QDialog', {
    props: ['modelValue'], render() { return this.modelValue ? h('div', { role: 'dialog' }, this.$slots.default?.()) : null }
  })
  app.component('QSelect', {
    props: ['modelValue', 'options'], render() {
      return h('select', { value: this.modelValue ?? '' }, this.options.map(({ label, value }) => h('option', { value: value ?? '' }, label)))
    }
  })
  app.component('QForm', { render() { return h('form', this.$slots.default?.()) } })
  app.component('QBtn', {
    props: ['label', 'disable'], render() { return h('button', { disabled: this.disable }, [this.label, this.$slots.default?.()]) }
  })
  for (const name of ['ripple', 'close-popup', 'confirm']) app.directive(name, {})
  app.mount(root)
  root.querySelector('.project-list-item > button').click()
  await nextTick()
})
afterEach(() => { app?.unmount(); vi.useRealTimers(); vi.unstubAllGlobals() })

it('badges unread notifications received in the background until read', async () => {
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
  const store = useInAppNotificationStore()
  notifications.fetchLatest.mockImplementationOnce(async () => { store.unreadCount = 3 })
  EventSource.instances[0].emit([{ resource: 'notifications' }])
  await vi.advanceTimersByTimeAsync(50)
  const favicon = document.querySelector('link[rel="icon"]')
  expect([document.title, favicon.getAttribute('href')]).toEqual(['(3) MatoLoop', '/icons/matoloop-unread.svg'])

  store.unreadCount = 0
  await nextTick()
  expect([document.title, favicon.getAttribute('href')]).toEqual(['MatoLoop', '/icons/matoloop.svg'])
  visibility.mockRestore()
})

it('refreshes only changed data through SSE without recurring REST polling', async () => {
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  const source = EventSource.instances[0]
  pending.fetchPending.mockClear()
  projects.fetchAll.mockClear()
  notifications.fetchLatest.mockClear()
  source.emit([{ resource: 'notifications' }])
  source.emit([{ resource: 'notifications' }])
  await vi.advanceTimersByTimeAsync(30_000)
  expect([notifications.fetchLatest.mock.calls.length, pending.fetchPending.mock.calls.length,
    projects.fetchAll.mock.calls.length]).toEqual([1, 0, 0])
  visibility.mockRestore()
})

it('retries a failed notification reload without polling unrelated resources', async () => {
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  notifications.fetchLatest.mockRejectedValueOnce(new Error('offline')).mockResolvedValue()
  projects.fetchAll.mockClear()
  pending.fetchPending.mockClear()
  EventSource.instances[0].emit([{ resource: 'notifications' }])
  await vi.advanceTimersByTimeAsync(50)
  await vi.advanceTimersByTimeAsync(31_000)
  expect([notifications.fetchLatest.mock.calls.length, projects.fetchAll.mock.calls.length,
    pending.fetchPending.mock.calls.length]).toEqual([2, 0, 0])
  visibility.mockRestore()
})

it('retains a session hint before its initial response has populated the current session', async () => {
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  useRoute().query = { session: 'standalone' }
  EventSource.instances[0].emit([{ resource: 'sessions', sessionId: 'standalone' }])
  await vi.advanceTimersByTimeAsync(50)
  expect(useChatStore().refreshSession).toHaveBeenCalledWith('standalone', { suppressErrorToast: true })
  visibility.mockRestore()
})

it('reconciles an external run hint and reloads on reconnect', async () => {
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  useChatStore().current = { id: 'standalone', messages: [] }
  useRoute().query = { session: 'standalone' }
  const source = EventSource.instances[0]
  source.emit([{ resource: 'sessions', sessionId: 'standalone' }])
  await vi.advanceTimersByTimeAsync(50)
  source.emit([{ resource: 'all' }])
  await vi.advanceTimersByTimeAsync(50)
  expect(useChatStore().refreshSession).toHaveBeenCalledTimes(2)
  visibility.mockRestore()
})

it.each([true, false])('refreshes changed grants only when the session app is visible (%s)', async (open) => {
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  useChatStore().current = { id: 'standalone', messages: [] }
  Object.assign(useControlCenterStore(), { open, activeContentId: 'session' })
  EventSource.instances[0].emit([{ resource: 'permissions', sessionId: 'standalone' }])
  await vi.advanceTimersByTimeAsync(50)
  expect(pending.fetchGrants.mock.calls).toEqual(open ? [[{ suppressErrorToast: true }, 'standalone']] : [])
  visibility.mockRestore()
})

it('opens notifications from the Control Center badge and otherwise restores the active app', async () => {
  const store = useInAppNotificationStore()
  const center = useControlCenterStore()
  center.activeContentId = 'session'
  store.unreadCount = 3
  await nextTick()
  root.querySelector('#control-center-toggle').click()
  await nextTick()
  expect(center.openApp).toHaveBeenLastCalledWith('notifications')
  center.open = false
  store.unreadCount = 0
  await nextTick()
  root.querySelector('#control-center-toggle').click()
  await nextTick()
  expect(center.openApp).toHaveBeenLastCalledWith('session')
  delete center.activeContentId
})

it.each([
  [true, false], [true, true], [false, false], [false, true]
])('restores the panel beside chat but not the fullscreen dialog (phone=%s, dark=%s)', async (phone, dark) => {
  quasar.dark.isActive = dark
  const center = useControlCenterStore()
  center.open = true
  center.restoredOpen = true
  await nextTick()
  expect(root.querySelector('#control-center-toggle')).toBeNull()

  quasar.screen.lt.sm = phone
  quasar.screen.lt.md = true
  await nextTick()
  expect(root.querySelector('[role="dialog"]')).toBeNull()
  const toggle = root.querySelector('#control-center-toggle')
  expect(toggle.getAttribute('aria-expanded')).toBe('false')

  toggle.click()
  await nextTick()
  expect(center.openApp).toHaveBeenCalledOnce()
  expect(root.querySelector('[role="dialog"]')).not.toBeNull()
})

it.each([true, false])('acknowledges only a focused visible session (unread=%s)', async (unread) => {
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  const focus = vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  notifications.markPresent.mockClear()
  useChatStore().current = { id: 'standalone', hasUnreadMessages: unread, messages: [] }
  useRoute().query = { session: 'standalone' }

  window.dispatchEvent(new Event('focus'))
  await vi.waitFor(() => expect(notifications.markPresent).toHaveBeenLastCalledWith(expect.objectContaining({ active: true, sessionId: 'standalone' })))
  expect(useChatStore().markSessionRead).toHaveBeenCalledWith(useChatStore().current)

  notifications.markPresent.mockClear()
  await vi.advanceTimersByTimeAsync(5000)
  expect(notifications.markPresent).toHaveBeenCalledWith(expect.objectContaining({ active: true, sessionId: 'standalone' }))

  notifications.markPresent.mockClear()
  focus.mockReturnValue(false)
  window.dispatchEvent(new Event('blur'))
  await vi.waitFor(() => expect(notifications.markPresent).toHaveBeenLastCalledWith(expect.objectContaining({ active: false, sessionId: null })))
  useChatStore().markSessionRead.mockClear()
  useChatStore().current = { ...useChatStore().current, messages: [{ id: 'new' }] }
  await nextTick()
  expect(useChatStore().markSessionRead).not.toHaveBeenCalled()

  focus.mockReturnValue(true)
  visibility.mockReturnValue('hidden')
  window.dispatchEvent(new Event('focus'))
  await vi.waitFor(() => expect(notifications.markPresent).toHaveBeenLastCalledWith(expect.objectContaining({ active: false, sessionId: null })))

  focus.mockRestore()
  visibility.mockRestore()
})

it.each([['Neue Session', null], ['Neue Session in Alpha', 'a']])('opens the blank new-session view from %s', async (label, projectId) => {
  root.querySelector(`[aria-label="${label}"]`).click()
  await vi.waitFor(() => expect(useRoute().query).toEqual({ ...(projectId ? { project: projectId } : {}), new: '1' }))
  expect(chat.loadSession).toHaveBeenCalledWith(null)
})

it('keeps project and session actions visible while their shared menu is open', async () => {
  const projectAction = root.querySelector('[aria-label="Aktionen für Alpha"]')
  projectAction.querySelector('.menu-stub__show').click()
  await nextTick()
  expect(projectAction.closest('.project-list-item').classList.contains('project-list-item--menu-open')).toBe(true)
  projectAction.querySelector('.menu-stub__hide').click()
  await nextTick()
  expect(projectAction.closest('.project-list-item').classList.contains('project-list-item--menu-open')).toBe(false)

  const sessionAction = root.querySelector('[aria-label="Aktionen für Project session"]')
  root.querySelector('button[aria-label="Project session anheften"]').click()
  await nextTick()
  expect(chat.setSessionPinned).toHaveBeenCalledWith('project-session', true)
  expect(useRoute().query).toEqual({})
  sessionAction.querySelector('.menu-stub__show').click()
  await nextTick()
  expect(sessionAction.closest('.session-list-item').classList.contains('session-list-item--menu-open')).toBe(true)
  expect(root.querySelector('.session-actions-menu [aria-label="Project session anheften"]')).toBeNull()
})

it.each([false, true])('opens the automation and respects blocked navigation when deleting (blocked=%s)', async (blocked) => {
  useChatStore().current = chat.cachedSessions[0]
  useRoute().query = { session: 'standalone' }
  useAutomationStore().items = [{ id: 'automation', executionSessionId: 'standalone', name: 'Bericht', revision: 4, enabled: true }]
  await nextTick()
  root.querySelector('#automation-list .session-list-item').click()
  await vi.waitFor(() => expect(useControlCenterStore().openApp).toHaveBeenCalledWith('automation'))
  expect(useRoute().query).toEqual({ session: 'standalone', automation: 'automation', controlCenter: 'automation' })
  root.querySelector('[aria-label="Aktionen für Bericht"] .menu-stub__show').click()
  await nextTick()
  confirm.mockResolvedValueOnce(true)
  if (blocked) routeUpdate.callback = vi.fn(() => false)
  const remove = [...root.querySelectorAll('.session-actions-menu [clickable]')].find((item) => item.textContent === 'Automation löschen')
  remove.click()
  if (blocked) {
    await vi.waitFor(() => expect(routeUpdate.callback).toHaveBeenCalled())
    expect(useAutomationStore().deleteAutomation).not.toHaveBeenCalled()
  } else {
    await vi.waitFor(() => expect(useAutomationStore().deleteAutomation).toHaveBeenCalledWith('automation', 4))
  }
  expect(useRoute().query.session).toBe('standalone')
})

it('shows compact activity times and prioritizes approval over the unread dot', async () => {
  const session = useChatStore().cachedSessions.find((item) => item.id === 'standalone')
  Object.assign(session, { hasUnreadMessages: true, lastMessageAt: new Date(Date.now() - 120_000).toISOString() })
  await nextTick()
  const row = source('standalone').closest('.session-list-item')
  expect([row.querySelector('time').textContent, row.querySelector('[role="status"]').getAttribute('aria-label')]).toEqual(['2m', 'Ungelesene Antwort'])
  session.activeRunStatus = 'waiting_approval'
  await nextTick()
  expect(row.querySelector('[role="status"]').getAttribute('aria-label')).toBe('Freigabe erforderlich')
  session.pinned = true
  await nextTick()
  expect(source('standalone').closest('.session-list-item').querySelector('time').textContent).toBe('2m')
  root.querySelector('button[aria-label="Standalone nicht mehr anheften"]').click()
  await nextTick()
  expect(chat.setSessionPinned).toHaveBeenCalledWith('standalone', false)
  Object.assign(session, { pinned: false, hasUnreadMessages: false, lastMessageAt: null, activeRunStatus: null })
})

it('scrolls an overflowing session title clear of the fade and button overlap', () => {
  const row = source('standalone').closest('.session-list-item')
  const title = row.querySelector('.session-list-item__title')
  const text = row.querySelector('.session-list-item__title-text')
  title.style.setProperty('--session-title-fade-width', '12px')
  title.style.setProperty('--session-title-fade-inset', '20px')
  Object.defineProperty(title, 'clientWidth', { value: 120 })
  vi.spyOn(text, 'getBoundingClientRect').mockReturnValue({ width: 260 })

  row.dispatchEvent(new Event('mouseenter'))

  expect(title.style.getPropertyValue('--session-title-scroll-distance')).toBe('-172px')
})

it.each([false, true])('beforeunload only warns about dirty editors (%s), leaving a cancelled exit connected', (dirty) => {
  workspace.hasDirtyEditors = dirty
  const event = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(event)
  expect(event.defaultPrevented).toBe(dirty)
  expect(terminal.release).not.toHaveBeenCalled()
  expect(terminal.close).not.toHaveBeenCalled()
})

it('evaluates the retained browser agent and tab, not the destination or the current=null load gap', async () => {
  const browser = useBrowserStore()
  const center = useControlCenterStore()
  useIntegrationStore().browser.instances = [{ id: 'browser-1', executionAgentId: 'agent-1', enabled: true }]
  Object.assign(browser, { sessionId: 'session-1', agentId: 'agent-1', integrationId: 'browser-1' })
  useChatStore().current = { id: 'session-2', preferredExecutionAgentId: 'offline-agent' }
  useRoute().query = { session: 'session-2' }
  center.sessionId = 'session-2'
  await nextTick()
  expect(browser.syncContext).toHaveBeenLastCalledWith({ sessionId: 'session-2', agentId: 'offline-agent', available: true, tabPresent: true })
  useChatStore().current = null
  await nextTick()
  expect(browser.syncContext).toHaveBeenLastCalledWith({ sessionId: undefined, agentId: undefined, available: true, tabPresent: true })
  useChatStore().current = { id: 'session-1', preferredExecutionAgentId: 'agent-1' }
  useRoute().query = { session: 'session-1' }
  center.sessionId = 'session-1'
  center.ready = false
  await nextTick()
  expect(browser.syncContext).toHaveBeenLastCalledWith({ sessionId: 'session-1', agentId: 'agent-1', available: true, tabPresent: true })
  center.ready = true
  await nextTick()
  expect(browser.syncContext).toHaveBeenLastCalledWith({ sessionId: 'session-1', agentId: 'agent-1', available: true, tabPresent: false })
  useIntegrationStore().browser.instances[0].enabled = false
  await nextTick()
  expect(browser.syncContext).toHaveBeenLastCalledWith({ sessionId: 'session-1', agentId: 'agent-1', available: false, tabPresent: false })
})

it('retains browser ownership through settings cache invalidation and failed catalog refresh, but accepts authoritative revocation', async () => {
  const browser = useBrowserStore()
  const integrations = useIntegrationStore()
  const instance = { id: 'browser-1', executionAgentId: 'agent-1', enabled: true }
  Object.assign(browser, { sessionId: 'session-1', agentId: 'agent-1', integrationId: 'browser-1' })
  integrations.browser.instances = [instance]
  useChatStore().current = { id: 'session-1', preferredExecutionAgentId: 'agent-1' }
  useRoute().query = { session: 'session-1' }
  useRoute().name = 'settings-integrations'
  // selectScope clears cached data before the independent personal browser fetch completes.
  integrations.browser.instances = []
  integrations.browserLoaded = false
  await nextTick()
  expect(browser.syncContext).toHaveBeenLastCalledWith({ sessionId: 'session-1', agentId: 'agent-1', available: true, tabPresent: true })
  integrations.browser.instances = [instance]
  integrations.browserLoaded = true
  integrations.catalogLoadFailed = true
  integrations.adapterAvailable.mockReturnValue(false)
  await nextTick()
  expect(browser.syncContext).toHaveBeenLastCalledWith({ sessionId: 'session-1', agentId: 'agent-1', available: true, tabPresent: true })
  integrations.catalogLoadFailed = false
  integrations.adapterAvailable.mockReturnValue(true)
  useRoute().name = 'chat'
  await nextTick()
  expect(browser.syncContext).toHaveBeenLastCalledWith({ sessionId: 'session-1', agentId: 'agent-1', available: true, tabPresent: true })
  integrations.browser.instances = [{ ...instance, enabled: false }]
  await nextTick()
  expect(browser.syncContext).toHaveBeenLastCalledWith({ sessionId: 'session-1', agentId: 'agent-1', available: false, tabPresent: true })
  integrations.browser.instances = []
  await nextTick()
  expect(browser.syncContext).toHaveBeenLastCalledWith({ sessionId: 'session-1', agentId: 'agent-1', available: false, tabPresent: true })
  integrations.browser.instances = [instance]
  integrations.adapterAvailable.mockReturnValue(false)
  await nextTick()
  expect(browser.syncContext).toHaveBeenLastCalledWith({ sessionId: 'session-1', agentId: 'agent-1', available: false, tabPresent: true })
})

function source(id) {
  const scope = id === 'standalone' ? '#standalone-session-list' : '.project-list-item'
  return root.querySelector(`${scope} .session-list-item__content`)
}
function target(id) {
  return id === null ? root.querySelector('[aria-labelledby="sessions-heading"]')
    : root.querySelectorAll('.project-list-item')[id === 'a' ? 0 : 1]
}
async function drag(element, type, dataTransfer, relatedTarget = null) {
  const event = new Event(type, { bubbles: true, cancelable: true })
  Object.assign(event, { dataTransfer, relatedTarget })
  element.dispatchEvent(event)
  await nextTick()
  return event
}

it.each([
  ['standalone', 'a'], ['project-session', 'b'], ['project-session', null]
])('drops %s onto %s into the move dialog, moving only on confirmation', async (id, destination) => {
  const transfer = { setData: vi.fn() }
  expect(source(id).getAttribute('draggable')).toBe('true')
  await drag(source(id), 'dragstart', transfer)
  expect(transfer.effectAllowed).toBe('move')
  expect(transfer.setData).toHaveBeenCalledWith('application/x-matoloop-session', id)
  expect((await drag(target(destination), 'dragover', transfer)).defaultPrevented).toBe(true)
  expect(transfer.dropEffect).toBe('move')
  expect(target(destination).classList.contains('app-surface-tonal')).toBe(true)
  expect((await drag(target(destination), 'drop', transfer)).defaultPrevented).toBe(true)
  expect(target(destination).classList.contains('app-surface-tonal')).toBe(false)
  const dialog = root.querySelector('[role="dialog"]')
  expect(dialog.textContent).toContain('Session verschieben')
  expect(dialog.querySelector('select').value).toBe(destination ?? '')
  expect(chat.moveSession).not.toHaveBeenCalled()
  dialog.querySelector('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  await nextTick()
  expect(chat.moveSession).toHaveBeenCalledExactlyOnceWith(id, destination)
})

it('keeps a move dialog active after closing the mobile drawer', async () => {
  quasar.screen.lt.md = true
  const transfer = { setData: vi.fn() }
  await drag(source('standalone'), 'dragstart', transfer)
  await drag(target('a'), 'drop', transfer)
  root.querySelector('[aria-label="Navigation schließen"]').click()
  await nextTick()

  expect(root.querySelector('#app-navigation-toggle')).not.toBeNull()
  const dialog = root.querySelector('[role="dialog"]')
  expect(dialog.textContent).toContain('Session verschieben')
  dialog.querySelector('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  await nextTick()
  expect(chat.moveSession).toHaveBeenCalledExactlyOnceWith('standalone', 'a')
})

it('moves the current session to another project without guarding its unchanged file root', async () => {
  useChatStore().current = { id: 'project-session', projectId: 'b' }
  useRoute().query = { session: 'project-session', project: 'a' }
  workspace.hasDirtyEditors = true
  const transfer = { setData: vi.fn() }
  await drag(source('project-session'), 'dragstart', transfer)
  await drag(target('b'), 'drop', transfer)

  root.querySelector('[role="dialog"] form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))

  await vi.waitFor(() => expect(useRoute().query).toEqual({ session: 'project-session', project: 'b' }))
  expect(confirm).not.toHaveBeenCalled()
  expect(workspace.hasDirtyEditors).toBe(true)
})

it.each([['standalone', null], ['project-session', 'a'], [null, 'b']])(
  'ignores same-destination or external drags: %s onto %s', async (id, destination) => {
    const transfer = { setData: vi.fn(), types: ['application/x-matoloop-session'], getData: () => 'standalone' }
    if (id) await drag(source(id), 'dragstart', transfer)
    expect((await drag(target(destination), 'dragover', transfer)).defaultPrevented).toBe(false)
    expect(target(destination).classList.contains('app-surface-tonal')).toBe(false)
    expect((await drag(target(destination), 'drop', transfer)).defaultPrevented).toBe(false)
    expect(root.querySelector('[role="dialog"]')).toBeNull()
    expect(chat.moveSession).not.toHaveBeenCalled()
  }
)

it('keeps highlighting within a target, clears it on leave, and clears drag state on dragend', async () => {
  const transfer = { setData: vi.fn() }
  await drag(source('project-session'), 'dragstart', transfer)
  const destination = target(null)
  await drag(destination, 'dragover', transfer)
  await drag(destination, 'dragleave', transfer, destination.firstElementChild)
  expect(destination.classList.contains('app-surface-tonal')).toBe(true)
  await drag(destination, 'dragleave', transfer)
  expect(destination.classList.contains('app-surface-tonal')).toBe(false)
  await drag(destination, 'dragover', transfer)
  expect(destination.classList.contains('app-surface-tonal')).toBe(true)
  await drag(source('project-session'), 'dragend', transfer)
  expect(destination.classList.contains('app-surface-tonal')).toBe(false)
  expect((await drag(destination, 'dragover', transfer)).defaultPrevented).toBe(false)
  expect((await drag(destination, 'drop', transfer)).defaultPrevented).toBe(false)
  expect(root.querySelector('[role="dialog"]')).toBeNull()
  expect(chat.moveSession).not.toHaveBeenCalled()
})
