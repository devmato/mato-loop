import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useRoute } from 'vue-router'
import { useAgentIdentityStore } from 'stores/agent-identity'
import { useApprovalStore } from 'stores/approvals'
import { useAutomationStore } from 'stores/automations'
import { useBrowserStore } from 'stores/browser'
import { useChatStore } from 'src/features/chat/store'
import { useControlCenterStore } from 'stores/control-center'
import { useExecutionAgentStore } from 'stores/execution-agents'
import { useInAppNotificationStore } from 'stores/in-app-notifications'
import { useIntegrationStore } from 'stores/integrations'
import { useProjectStore } from 'stores/projects'
import { useQuestionStore } from 'stores/questions'
import { useSearchStore } from 'stores/search'
import { useTerminalStore } from 'stores/terminal'
import { useWorkspaceStore } from 'stores/workspace'
import { useHomeStore } from 'src/features/home/store'
import { applyBranding, useAppearanceStore } from 'src/features/settings/appearance-store'
import { terminalControlCenterState } from 'src/app/control-center/manifest'
import { isPwaUpdateReload } from 'src/utils/pwa-update'
import { searchShortcutAction } from 'src/utils/search-shortcut'
import { randomUuid } from 'src/utils/random-id'
import { notifyError } from 'src/utils/notifications'

const PRESENCE_REFRESH_MS = 5000

export function useShellSync({ drawerOpen, controlCenterVisible }) {
  const $q = useQuasar()
  const route = useRoute()
  const identity = useAgentIdentityStore()
  const approvals = useApprovalStore()
  const automations = useAutomationStore()
  const browser = useBrowserStore()
  const chat = useChatStore()
  const home = useHomeStore()
  const controlCenter = useControlCenterStore()
  const executionAgents = useExecutionAgentStore()
  const notifications = useInAppNotificationStore()
  const appearance = useAppearanceStore()
  const integrations = useIntegrationStore()
  const projects = useProjectStore()
  const questions = useQuestionStore()
  const search = useSearchStore()
  const terminal = useTerminalStore()
  const workspace = useWorkspaceStore()
  const routeSessionId = computed(() => (
    typeof route.query.session === 'string' ? route.query.session : null
  ))
  const terminalContextState = computed(() => terminalControlCenterState(chat.current, executionAgents))
  const visualViewportHeight = ref(null)
  const visualViewportOffsetTop = ref(0)
  const visualViewportStyle = computed(() => {
    if (route.name !== 'chat' || visualViewportHeight.value === null) return null
    return {
      '--app-visual-viewport-height': `${visualViewportHeight.value}px`,
      '--app-visual-viewport-offset-top': `${visualViewportOffsetTop.value}px`
    }
  })
  let presenceTimer = null
  let changeSource = null
  let heartbeatTimer = null
  let refreshTimer = null
  let retryDelay = 1000
  let retryWaiting = false
  let refreshing = false
  let disposed = false
  const pendingChanges = new Map()
  const presenceClientId = randomUuid(window.crypto)
  const focused = ref(appIsFocused())
  const visibleSessionId = computed(() => (
    focused.value && route.name === 'chat' && !route.query.subagent
    && routeSessionId.value === chat.current?.id
    && !(controlCenterVisible.value && $q.screen.lt.md)
    && !(drawerOpen.value && $q.screen.lt.md)
      ? chat.current.id : null
  ))
  let presenceRequest = Promise.resolve()

  function updateVisualViewport() {
    const viewport = window.visualViewport
    const height = viewport?.height || window.innerHeight
    const offsetTop = viewport?.offsetTop || 0
    if (Number.isFinite(height) && height > 0) visualViewportHeight.value = height
    visualViewportOffsetTop.value = Number.isFinite(offsetTop) ? Math.max(0, offsetTop) : 0
  }

  function handleSearchShortcut(event) {
    const action = searchShortcutAction(event, search.dialogOpen)
    if (action === 'app') {
      event.preventDefault()
      search.openDialog()
    } else if (action === 'browser') {
      search.dialogOpen = false
    }
  }

  function appIsFocused() {
    return document.visibilityState === 'visible' && document.hasFocus()
  }

  function refreshAppPresence() {
    focused.value = appIsFocused()
    const presence = { clientId: presenceClientId, sessionId: visibleSessionId.value, active: focused.value }
    presenceRequest = presenceRequest.catch(() => {}).then(() => notifications.markPresent(presence)).catch(() => {})
  }

  function refreshSessionsWhenVisible() {
    refreshAppPresence()
    if (document.visibilityState !== 'visible') return
    queueChanges([{ resource: 'all' }])
  }

  function refreshPushState() {
    void notifications.reconcilePush({ suppressErrorToast: true }).catch(() => {})
  }

  function handlePushMessage(event) {
    if (event.data?.type !== 'matoloop:push' || event.data.version !== 1) return
    queueChanges([{ resource: 'notifications' }, { resource: 'projects' }, { resource: 'approvals' }, { resource: 'questions' }])
  }

  function queueChanges(changes) {
    if (disposed) return
    if (document.visibilityState !== 'visible') {
      changes = changes.some((change) => ['all', 'notifications'].includes(change.resource))
        ? [{ resource: 'notifications' }] : []
    }
    if (!changes.length) return
    for (const change of changes) pendingChanges.set(`${change.resource}:${change.sessionId || ''}`, change)
    if (retryWaiting) {
      window.clearTimeout(refreshTimer)
      refreshTimer = null
      retryWaiting = false
    }
    if (!refreshing && refreshTimer === null) refreshTimer = window.setTimeout(refreshChanges, 50)
  }

  async function refreshChanges() {
    refreshTimer = null
    if (disposed) return
    const changes = [...pendingChanges.values()].filter((change) => (
      document.visibilityState === 'visible' || change.resource === 'notifications'
    ))
    pendingChanges.clear()
    const resources = new Set(changes.map((change) => change.resource))
    const all = resources.has('all')
    const sessionsChanged = all || resources.has('sessions') || resources.has('messages')
    const sessionIds = new Set(changes.map((change) => change.sessionId).filter(Boolean))
    const config = { suppressErrorToast: true }
    const requests = []
    const reload = (resource, request, sessionId = null) => requests.push({ resource, sessionId, request })
    if (all || resources.has('automations')) reload('automations', automations.fetchAutomations(config))
    if (all || sessionsChanged || resources.has('projects')) reload('projects', projects.fetchAll(config))
    if (all || resources.has('approvals')) reload('approvals', approvals.fetchPending(config))
    if (all || resources.has('questions')) reload('questions', questions.fetchPending(config))
    if (all || resources.has('notifications')) reload('notifications', notifications.fetchLatest(config))
    if (sessionsChanged) {
      if (route.name === 'chat' && !routeSessionId.value && !Object.hasOwn(route.query, 'new')) reload('messages', home.load(config))
      if (route.name === 'chat' && routeSessionId.value && !chat.running
        && (all || !sessionIds.size || sessionIds.has(routeSessionId.value))) {
        reload('messages', chat.refreshSession(routeSessionId.value, config), routeSessionId.value)
      }
      for (const [scope, list] of Object.entries(chat.sessionLists)) {
        if ((list.loaded || list.loadRequest) && (all || resources.has('sessions') || list.items.some((session) => sessionIds.has(session.id)))) {
          reload('sessions', chat.refreshSessions(config, scope === 'standalone' ? null : scope))
        }
      }
    }
    if ((all || resources.has('permissions')) && controlCenterVisible.value && controlCenter.activeContentId === 'session'
      && chat.current?.id && (all || sessionIds.has(chat.current.id))) reload('permissions', approvals.fetchGrants(config, chat.current.id), chat.current.id)
    refreshing = true
    const results = await Promise.allSettled(requests.map((item) => item.request))
    refreshing = false
    if (disposed) return
    const hasNewChanges = pendingChanges.size > 0
    const failure = results.find((result) => result.status === 'rejected')
    if (failure && retryDelay === 1000) notifyError(failure.reason)
    let retry = false
    results.forEach((result, index) => {
      if (result.status !== 'rejected') return
      const status = result.reason?.response?.status
      if (status == null || status === 408 || status === 429 || status >= 500) {
        const { resource, sessionId } = requests[index]
        pendingChanges.set(`${resource}:${sessionId || ''}`, { resource, sessionId })
        retry = true
      }
    })
    retryWaiting = retry && !hasNewChanges
    if (pendingChanges.size) refreshTimer = window.setTimeout(refreshChanges, retryWaiting ? retryDelay : 50)
    retryDelay = retry ? Math.min(retryDelay * 2, 30_000) : 1000
  }

  function connectChanges() {
    if (disposed) return
    changeSource?.close()
    const source = new EventSource('/api/events')
    changeSource = source
    const heartbeat = () => {
      if (disposed || changeSource !== source) return
      window.clearTimeout(heartbeatTimer)
      heartbeatTimer = window.setTimeout(connectChanges, 45_000)
    }
    source.onopen = heartbeat
    source.addEventListener('keepalive', heartbeat)
    source.addEventListener('changed', (event) => {
      if (disposed || changeSource !== source) return
      heartbeat()
      queueChanges(JSON.parse(event.data))
    })
    heartbeat()
  }

  function preventDiscardOnUnload(event) {
    if (isPwaUpdateReload() || !workspace.hasDirtyEditors) return
    event.preventDefault()
    event.returnValue = ''
  }

  watch(() => [notifications.unreadCount, appearance.appName, appearance.description, appearance.logoUrl, appearance.customLogo],
    () => applyBranding(appearance, notifications.unreadCount), { immediate: true })

  watch(visibleSessionId, refreshAppPresence)
  watch(() => [visibleSessionId.value, chat.current?.hasUnreadMessages, chat.current?.messages?.length], ([id, unread], previous = []) => {
    if (id && (unread || id !== previous[0])) void chat.markSessionRead(chat.current).catch(() => {})
  }, { immediate: true, flush: 'post' })

  watch(
    () => [controlCenterVisible.value, $q.screen.lt.lg],
    ([open, narrow]) => {
      if (open && narrow) drawerOpen.value = false
    }
  )

  watch(
    () => [
      chat.current?.id || null,
      chat.current?.preferredExecutionAgentId || null,
      terminalContextState.value.agent?.connected === true,
      terminalContextState.value.available,
      controlCenter.tabs.some((tab) => tab.appId === 'terminal'),
      route.name === 'chat'
    ],
    ([sessionId, agentId, connected, supported, tabPresent, onChatRoute]) => {
      terminal.syncContext({ sessionId, agentId, connected, supported, tabPresent, onChatRoute })
    },
    { immediate: true }
  )

  watch(() => route.name, (name) => {
    if (name === 'chat') void Promise.allSettled([
      integrations.fetchCatalog(), integrations.fetchBrowser(), integrations.fetchTbsDesk()
    ])
  }, { immediate: true })

  watch([() => chat.current?.id, () => controlCenter.ready,
    () => automations.items.some((item) => item.executionSessionId === chat.current?.id)],
  ([id, ready, configured]) => {
    if (id && ready && configured) void controlCenter.ensureApp('automation')?.catch(() => {})
  }, { immediate: true })

  watch(() => route.name === 'chat' && routeSessionId.value === chat.current?.id ? chat.current?.id : null,
    (id) => browser.setDisplayedSession(id), { immediate: true, flush: 'sync' })

  watch(() => [
    chat.current?.id, chat.current?.preferredExecutionAgentId, browser.sessionId,
    executionAgents.byId(browser.agentId)?.connected === true
      && (!integrations.catalogLoaded || integrations.catalogLoadFailed || integrations.adapterAvailable('browser', 'personal'))
      && (!integrations.browserLoaded || integrations.browser.instances.some((item) => item.id === browser.integrationId
        && item.executionAgentId === browser.agentId && item.enabled)),
    controlCenter.sessionId === browser.sessionId && controlCenter.ready
      ? controlCenter.confirmedState.tabs.some((tab) => tab.appId === 'browser') : true
  ], ([sessionId, agentId, , available, tabPresent]) => {
    browser.syncContext({ sessionId, agentId, available, tabPresent })
  }, { immediate: true, flush: 'post' })

  onMounted(async () => {
    updateVisualViewport()
    window.visualViewport?.addEventListener('resize', updateVisualViewport)
    window.visualViewport?.addEventListener('scroll', updateVisualViewport)
    window.addEventListener('resize', updateVisualViewport)
    window.addEventListener('keydown', handleSearchShortcut)
    window.addEventListener('focus', refreshAppPresence)
    window.addEventListener('blur', refreshAppPresence)
    window.addEventListener('beforeunload', preventDiscardOnUnload)
    document.addEventListener('visibilitychange', refreshSessionsWhenVisible)
    window.addEventListener('matoloop:service-worker-ready', refreshPushState)
    window.navigator.serviceWorker?.addEventListener('message', handlePushMessage)
    presenceTimer = window.setInterval(() => {
      if (document.visibilityState === 'visible') refreshAppPresence()
    }, PRESENCE_REFRESH_MS)
    connectChanges()
    refreshAppPresence()
    await Promise.allSettled([
      notifications.reconcilePush({ suppressErrorToast: true }),
      identity.fetchIdentity()
    ])
  })

  onBeforeUnmount(() => {
    disposed = true
    changeSource?.close()
    window.clearTimeout(heartbeatTimer)
    window.clearTimeout(refreshTimer)
    applyBranding(appearance)
    window.visualViewport?.removeEventListener('resize', updateVisualViewport)
    window.visualViewport?.removeEventListener('scroll', updateVisualViewport)
    window.removeEventListener('resize', updateVisualViewport)
    window.removeEventListener('keydown', handleSearchShortcut)
    window.removeEventListener('focus', refreshAppPresence)
    window.removeEventListener('blur', refreshAppPresence)
    presenceRequest = presenceRequest.catch(() => {}).then(() => notifications.markPresent({
      clientId: presenceClientId, sessionId: null, active: false
    })).catch(() => {})
    window.removeEventListener('beforeunload', preventDiscardOnUnload)
    terminal.release()
    browser.release()
    document.removeEventListener('visibilitychange', refreshSessionsWhenVisible)
    window.removeEventListener('matoloop:service-worker-ready', refreshPushState)
    window.navigator.serviceWorker?.removeEventListener('message', handlePushMessage)
    window.clearInterval(presenceTimer)
  })

  return { visualViewportStyle }
}
