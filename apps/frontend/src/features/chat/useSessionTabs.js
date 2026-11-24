import { computed, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useRouter } from 'vue-router'
import { useUserPreferencesStore } from 'src/features/settings/user-preferences-store'
import { sessionTabKey, sessionTabTarget, useSessionTabsStore } from './session-tabs-store'

export function useSessionTabNavigation() {
  const preferences = useUserPreferencesStore()
  const tabs = useSessionTabsStore()
  const $q = useQuasar()
  const router = useRouter()
  const enabled = computed(() => preferences.sessionTabsEnabled && !$q.screen.lt.sm)
  async function openKeptSession(session) {
    if (!enabled.value || !session.id) return
    const generation = tabs.generation
    if (tabs.owner && !tabs.loaded) await tabs.fetchWorkspace()
    if (generation !== tabs.generation) return
    const failure = await router.push(sessionTabTarget({ sessionId: session.id, projectId: session.projectId }))
    if (generation !== tabs.generation) return
    if (!failure || router.currentRoute.value.query.session === session.id) tabs.visit(session, { keep: true })
  }
  const position = computed(() => preferences.sessionTabsPosition)
  return { enabled, tabs, position, openKeptSession }
}

export function useSessionTabs({ chat, route }) {
  const navigation = useSessionTabNavigation()
  const { enabled, tabs } = navigation
  const preferences = useUserPreferencesStore()
  const context = computed(() => chat.current || {
    projectId: typeof route.query.project === 'string' ? route.query.project : null
  })
  const routeKey = computed(() => route.query.session || (Object.hasOwn(route.query, 'new')
    ? sessionTabKey(null, route.query.project) : null))
  // Show the accepted navigation immediately, independently of the detail request.
  const activeKey = computed(() => routeKey.value || sessionTabKey(context.value.id, context.value.projectId))
  let restoringKey = route.meta?.restoreSessionTabs ? routeKey.value : null
  watch(routeKey, () => { restoringKey = null }, { flush: 'sync' })
  function visit(keep) {
    if (!enabled.value || chat.loading || chat.loadError) return
    if (tabs.owner && !tabs.loaded) return
    if ((route.query.session || null) !== (chat.current?.id || null)) return
    if (!chat.current && !Object.hasOwn(route.query, 'new')) return
    if (restoringKey === activeKey.value) {
      // Leave the chat accessible without evicting any saved tab on reload.
      if (tabs.items.length >= preferences.sessionTabsLimit && !tabs.items.some(tab => tab.key === activeKey.value)) return
      // A restored preview must not be replaced by the previous device's URL.
      keep ||= tabs.items.some(tab => tab.preview && tab.key !== activeKey.value)
      restoringKey = null
    }
    tabs.visit(context.value, { keep })
  }
  const hasDraft = computed(() => Boolean(chat.activeDraft) || chat.activeAttachmentDraft.length > 0)
  watch([
    enabled, () => tabs.loaded, () => chat.loading, () => chat.current?.id, () => chat.current?.title,
    () => chat.current?.projectId, () => route.query.session, () => route.fullPath, hasDraft, () => chat.running
  ], () => {
    visit(hasDraft.value || chat.running)
  }, { immediate: true })
  watch(hasDraft, (present) => {
    if (present) visit(true)
  }, { flush: 'sync' })
  return { ...navigation, activeKey }
}
