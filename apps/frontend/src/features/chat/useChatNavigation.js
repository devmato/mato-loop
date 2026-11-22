import { computed, nextTick, onBeforeUnmount, watch } from 'vue'
import { useSessionTabsStore } from './session-tabs-store'

export function useChatNavigation({ chat, route, router, composer, controlCenter, subagents, projects }) {
  const queryId = key => computed(() => typeof route.query[key] === 'string' ? route.query[key] : null)
  const routeSessionId = queryId('session')
  const routeProjectId = queryId('project')
  const routeMessageId = queryId('message')
  const routeSubagentId = queryId('subagent')
  const newSessionRequested = computed(() => Object.hasOwn(route.query, 'new'))
  const personalRequested = computed(() => Object.hasOwn(route.query, 'personal')
    && !['session', 'new', 'project'].some(key => Object.hasOwn(route.query, key)))
  const destination = computed(() => routeSessionId.value ? `session:${routeSessionId.value}`
    : personalRequested.value ? 'personal' : `draft:${route.fullPath}`)
  let operation
  let disposed = false

  async function reconcileProject() {
    if (chat.current?.id !== routeSessionId.value || !chat.current
      || (chat.current.projectId || null) === routeProjectId.value) return
    const query = { ...route.query }
    if (chat.current.projectId) query.project = chat.current.projectId
    else delete query.project
    await router.replace({ name: 'chat', query })
  }

  async function loadRouteSession() {
    const request = { destination: destination.value, owner: chat.draftOwnerUserId }
    operation = request
    const current = () => !disposed && operation === request
      && destination.value === request.destination && chat.draftOwnerUserId === request.owner
    const id = routeSessionId.value
    await composer.value?.prepareAudioNavigation?.()
    if (!current()) return
    if (personalRequested.value) {
      await chat.loadSession(null)
      if (!current()) return
      chat.loading = true
      try {
        const session = await chat.ensurePersonalChat()
        if (current() && session) {
          const query = { ...route.query, session }
          delete query.personal
          await router.replace({ name: 'chat', query })
        }
      } catch (error) {
        if (current()) chat.loadError = error
      } finally {
        if (current()) chat.loading = false
      }
      return
    }
    const session = await chat.loadSession(id).catch(() => null)
    if (!current()) return
    if (session?.readOnly && session.parentSessionId) {
      await router.replace({ name: 'chat', query: { session: session.parentSessionId, subagent: session.id } })
      return
    }
    if (chat.selectedSessionId !== id || chat.loadError) return
    await reconcileProject()
    if (!current()) return
    if (typeof route.query.controlCenter === 'string') {
      try { await controlCenter.openApp(route.query.controlCenter) } catch { /* Central API toast. */ }
    }
    await nextTick()
    if (current() && !routeMessageId.value && (id || newSessionRequested.value)) composer.value?.focus({ automatic: true })
  }

  watch(destination, (_, previous) => {
    // Attaching a just-created session preserves its composer and pending message DOM.
    if (routeSessionId.value && previous?.startsWith('draft:') && chat.current?.id === routeSessionId.value) return
    void loadRouteSession()
  }, { immediate: true })
  watch(routeProjectId, () => { void reconcileProject() })
  watch(routeSessionId, id => { void subagents.loadChildren(id) }, { immediate: true })
  watch(() => chat.createdSessionResult, async result => {
    if (!result) return
    if (!routeSessionId.value) useSessionTabsStore().attachDraft(result.sessionId, result.projectId || routeProjectId.value)
    chat.createdSessionResult = null
    if (routeProjectId.value) void projects.fetchAll().catch(() => {})
    await router.replace({ name: 'chat', query: {
      ...(result.projectId || routeProjectId.value ? { project: result.projectId || routeProjectId.value } : {}),
      session: result.sessionId,
      ...(result.appId ? { controlCenter: result.appId } : {}),
      ...(result.appId === 'automation' ? { automation: 'new' } : {})
    } })
  })
  onBeforeUnmount(() => { disposed = true; operation = null; void subagents.loadChildren(null) })
  return { routeSessionId, routeProjectId, routeMessageId, routeSubagentId, newSessionRequested, loadRouteSession }
}
