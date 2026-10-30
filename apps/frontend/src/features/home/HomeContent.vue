<template>
  <section class="chat-home q-mx-auto" :class="$q.screen.lt.md ? 'q-mt-none' : 'q-mt-md'" aria-label="Home">
    <header class="text-center q-px-md" :class="{ 'q-pt-sm': $q.screen.lt.md }">
      <AppLogo :size="36" />
      <h1 class="text-h5 text-weight-bold q-mt-md q-mb-sm">{{ greeting }}</h1>
      <p class="text-body1 app-muted q-mt-none q-mb-sm">Was möchtest du tun?</p>
    </header>
    <div class="q-pa-md">
      <q-banner v-if="failedSources.length" rounded class="app-surface-tonal q-mb-md" role="status">
        Nicht aktuell verfügbar: {{ failedSources.join(', ') }}.
        <template #action><q-btn flat no-caps label="Erneut laden" :loading="refreshing" @click="refresh" /></template>
      </q-banner>
      <section aria-labelledby="home-sessions">
        <h2 id="home-sessions" class="text-weight-medium q-mt-none q-mb-xs" :class="$q.screen.lt.sm ? 'text-subtitle2' : 'text-subtitle1'">Zuletzt aktiv</h2>
        <div v-if="home.loading && !home.loaded" aria-label="Sessions werden geladen">
          <q-skeleton v-for="index in 3" :key="index" type="text" class="q-my-md" />
        </div>
        <p v-else-if="home.loadFailed" class="app-muted">Sessions konnten nicht geladen werden.</p>
        <q-list v-else-if="recentSessions.length">
          <q-item v-for="session in recentSessions" :key="session.id" clickable v-ripple :dense="$q.screen.lt.sm" :active="false" @click="router.push(sessionRoute(session))" class="home-activity-row rounded-borders q-px-sm" :class="[$q.screen.lt.sm ? 'q-py-xs' : 'q-py-sm', { 'home-activity-row--menu-open': openSessionMenuId === session.id }]">
            <q-item-section side class="q-pr-sm">
              <span class="home-session-status row items-center justify-center relative-position" :role="session.status ? 'status' : undefined" :aria-label="session.status?.label">
                <template v-if="session.status?.kind === 'approval'">
                  <q-icon name="pending_actions" color="warning" size="16px" />
                  <q-badge v-if="session.status.count > 1" floating rounded color="warning" text-color="dark" :label="session.status.count" />
                </template>
                <q-spinner-grid v-else-if="session.status?.kind === 'active'" color="primary" size="16px" />
                <q-icon v-else-if="session.status?.kind === 'unread'" name="fiber_manual_record" color="primary" size="12px" />
                <q-icon v-else name="chat_bubble_outline" color="primary" size="20px" />
                <q-tooltip v-if="session.status && $q.platform.is.mobile !== true">{{ session.status.label }}</q-tooltip>
              </span>
            </q-item-section>
            <q-item-section>
              <q-item-label lines="1" :class="{ 'text-weight-medium': session.status?.kind === 'unread' }">
                {{ session.title }}
              </q-item-label>
              <q-item-label caption>{{ projectName(session.projectId) }} · {{ formatRelativeDateTime(session.lastMessageAt || session.createdAt) }}</q-item-label>
            </q-item-section>
            <q-item-section side class="q-pl-sm" @click.stop @keydown.stop @keyup.stop>
              <SidebarActionMenu class="home-activity-row__menu" :model-value="openSessionMenuId === session.id" @update:model-value="openSessionMenuId = $event ? session.id : null" :aria-label="`Aktionen für ${session.title}`" list-class="session-actions-menu">
                <SessionMenuItems :session="session" show-pin :pinning="pinningSessionId === session.id" @toggle-pin="toggleSessionPinned" @rename="openRenameDialog" @move="openMoveDialog" @archive="archiveSession" @delete="removeSession" />
              </SidebarActionMenu>
            </q-item-section>
          </q-item>
        </q-list>
        <p v-else class="app-muted q-mt-sm">Deine letzten Gespräche erscheinen hier.</p>
      </section>
      <section v-if="ready(automations) && upcoming.length" class="q-mt-lg" aria-labelledby="home-automations">
        <h2 id="home-automations" class="text-subtitle2 app-muted q-mt-none q-mb-xs">Nächste Automationen</h2>
        <q-list>
          <q-item v-for="automation in upcoming" :key="automation.id" clickable v-ripple @click="router.push({ name: 'settings-automation-detail', params: { automationId: automation.id } })" class="home-activity-row rounded-borders q-px-sm" :class="{ 'home-activity-row--menu-open': openAutomationMenuId === automation.id }">
            <q-item-section side class="q-pr-sm"><q-icon name="auto_awesome" color="primary" size="20px" /></q-item-section>
            <q-item-section>
              <q-item-label lines="1">{{ automation.name }}</q-item-label>
              <q-item-label caption>{{ formatAbsoluteDateTime(automation.effectiveState.nextRunAt) }}</q-item-label>
            </q-item-section>
            <q-item-section side class="q-pl-sm" @click.stop @keydown.stop @keyup.stop>
              <SidebarActionMenu class="home-activity-row__menu" :model-value="openAutomationMenuId === automation.id" @update:model-value="openAutomationMenuId = $event ? automation.id : null" :aria-label="`Aktionen für ${automation.name}`" list-class="session-actions-menu">
                <AutomationMenuItems :automation="automation" @edit="router.push({ name: 'settings-automation-detail', params: { automationId: automation.id } })" />
              </SidebarActionMenu>
            </q-item-section>
          </q-item>
        </q-list>
      </section>
    </div>
    <SessionDialogs
      v-model:rename-open="renameDialogOpen" v-model:rename-title="renameTitle"
      v-model:move-open="moveDialogOpen" v-model:move-project-id="moveProjectId"
      :renaming="renaming" :moving-session="movingSession" :move-source-project-id="moveSourceProjectId"
      :project-options="projectOptions" :required-rule="requiredRule" @rename="renameSession" @move="moveSession"
    />
  </section>
</template>

<script setup>
import { computed, inject, onMounted, ref } from 'vue'
import { useQuasar } from 'quasar'
import { useRoute, useRouter } from 'vue-router'
import AppLogo from 'src/shared/ui/AppLogo.vue'
import SidebarActionMenu from 'src/features/chat/components/SidebarActionMenu.vue'
import SessionMenuItems from 'src/features/chat/components/SessionMenuItems.vue'
import SessionDialogs from 'src/features/chat/components/SessionDialogs.vue'
import AutomationMenuItems from 'src/features/automations/AutomationMenuItems.vue'
import { useSidebarSessions } from 'src/features/chat/useSidebarSessions'
import { useChatStore } from 'src/features/chat/store'
import { useControlCenterStore } from 'stores/control-center'
import { useWorkspaceStore } from 'stores/workspace'
import { useConfirm } from 'src/composables/confirm'
import { useApprovalStore } from 'stores/approvals'
import { useAutomationStore } from 'stores/automations'
import { useExecutionAgentStore } from 'stores/execution-agents'
import { useInAppNotificationStore } from 'stores/in-app-notifications'
import { useProjectStore } from 'stores/projects'
import { useQuestionStore } from 'stores/questions'
import { useUserProfileStore } from 'stores/user-profile'
import { formatAbsoluteDateTime, formatRelativeDateTime } from 'src/utils/date-time'
import { sessionSidebarStatus } from 'src/features/chat/session-groups'
import { useHomeStore } from './store'

const home = useHomeStore()
const projects = useProjectStore()
const automations = useAutomationStore()
const computers = useExecutionAgentStore()
const approvals = useApprovalStore()
const questions = useQuestionStore()
const notifications = useInAppNotificationStore()
const profile = useUserProfileStore()
const route = useRoute()
const router = useRouter()
const $q = useQuasar()
const { confirm } = useConfirm()
const confirmDiscardedEdit = inject('confirmDiscardedEdit')
const openSessionMenuId = ref(null)
const openAutomationMenuId = ref(null)
const {
  projectOptions, renameDialogOpen, renameTitle, renaming, pinningSessionId,
  moveDialogOpen, moveSourceProjectId, moveProjectId, movingSession,
  openRenameDialog, openMoveDialog, moveSession, renameSession, toggleSessionPinned,
  removeSession, archiveSession
} = useSidebarSessions({
  chat: useChatStore(), projects, approvals, questions, workspace: useWorkspaceStore(),
  controlCenter: useControlCenterStore(), automations, route, router, $q, confirm,
  drawerOpen: ref(false), selectedProjectId: computed(() => route.query.project || null), confirmDiscardedEdit
})
const requiredRule = (value) => Boolean(String(value || '').trim()) || 'Pflichtfeld'
const refreshing = ref(false)
const ready = (store) => store.loaded && !store.loadFailed
const greeting = computed(() => {
  const name = ready(profile) && [profile.preferredName, profile.fullName]
    .flatMap((field) => [field.privateOnly, field.both, field.publicOnly])
    .find((value) => value?.trim())?.trim()
  return name ? `Willkommen zurück, ${name}` : 'Willkommen zurück'
})
const failedSources = computed(() => [
  ['Sessions', home], ['Projekte', projects], ['Automationen', automations],
  ['Computer', computers], ['Freigaben', approvals], ['Rückfragen', questions], ['Benachrichtigungen', notifications], ['Profil', profile]
].filter(([, store]) => store.loadFailed).map(([label]) => label))
const recentSessions = computed(() => home.sessions.map((session) => ({
  ...session,
  status: sessionSidebarStatus(session, approvals.forSession(session.id).length, questions.forSession(session.id).length)
    || (session.hasUnreadMessages ? { kind: 'unread', label: 'Ungelesene Antwort' } : null)
})))
const upcoming = computed(() => automations.items
  .filter((item) => item.effectiveState?.enabled && item.effectiveState.nextRunAt)
  .sort((left, right) => new Date(left.effectiveState.nextRunAt) - new Date(right.effectiveState.nextRunAt))
  .slice(0, 2))
const projectName = (id) => id ? projects.items.find((item) => item.id === id)?.name || 'Projekt' : 'Ohne Projekt'
const sessionRoute = (session) => ({ name: 'chat', query: { session: session.id, ...(session.projectId ? { project: session.projectId } : {}) } })

async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await Promise.allSettled([
      home.load(), projects.fetchAll(), automations.fetchAutomations(),
      computers.fetchAll(), approvals.fetchPending(), questions.fetchPending(), notifications.fetchLatest(), profile.fetchProfile()
    ])
  } finally {
    refreshing.value = false
  }
}

onMounted(() => { void Promise.allSettled([home.load(), profile.fetchProfile()]) })
</script>

<style scoped>
.chat-home {
  width: min(900px, 100%);
  margin-bottom: 24px;
}

.home-session-status {
  width: 20px;
  height: 20px;
}
</style>
