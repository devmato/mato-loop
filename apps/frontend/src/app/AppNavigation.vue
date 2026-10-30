<template>
  <q-drawer
    id="app-navigation-drawer"
    v-model="drawerOpen"
    show-if-above
    bordered
    :width="288"
    :breakpoint="1023"
    class="app-drawer"
  >
    <div class="column full-height no-wrap drawer-layout">
      <q-toolbar class="drawer-toolbar q-px-sm">
        <router-link
          to="/"
          class="app-brand col row no-wrap items-center q-gutter-sm"
          @click="closeDrawerOnMobile"
        >
          <AppLogo />
          <div class="app-brand__name ellipsis">{{ appearance.appName }}</div>
        </router-link>

        <SidebarIconButton icon="search" aria-label="Suche öffnen" @click="search.openDialog" />
        <SidebarIconButton icon="menu" aria-label="Navigation schließen" @click="closeNavigation" />
      </q-toolbar>
      <q-separator class="lt-md" />

      <div class="col column no-wrap drawer-navigation q-py-xs">
        <SidebarNavItem icon="home" icon-color="primary" label="Home" :to="{ name: 'chat' }" :active="route.name === 'chat' && !routeSessionId && !Object.hasOwn(route.query, 'new')" class="q-mx-xs q-mb-xs" @click="closeDrawerOnMobile" />
        <q-scroll-area
          class="col session-scroll-area"
          :content-style="{ width: '100%' }"
          :content-active-style="{ width: '100%' }"
          tabindex="0"
          aria-label="Projekte und Sessions"
        >
          <section aria-labelledby="automations-heading" class="q-mx-xs q-mb-xs">
            <SidebarNavItem
              icon="auto_awesome" icon-color="primary" label="Automationen" label-id="automations-heading" :expanded="automationsSectionOpen"
              aria-controls="automation-list" :aria-expanded="automationsSectionOpen"
              @click="automationsSectionOpen = !automationsSectionOpen"
            >
              <template #label-suffix>
                <span class="q-ml-sm" :aria-label="automations.loadFailed ? 'Automationen konnten nicht geladen werden' : `${automations.items.length} Automationen`">
                  <q-icon v-if="automations.loadFailed" name="error_outline" color="warning" size="16px" />
                  <q-badge v-else-if="automations.loaded" :color="$q.dark.isActive ? 'grey-9' : 'grey-3'" :text-color="$q.dark.isActive ? 'grey-4' : 'grey-8'" :label="automations.items.length" />
                </span>
              </template>
              <template #actions>
                <SidebarIconButton class="sidebar-section__action" icon="add" aria-label="Automation erstellen" @click="createAutomation" />
              </template>
            </SidebarNavItem>
            <q-list v-show="automationsSectionOpen" id="automation-list">
              <SidebarNavItem v-if="automations.loadFailed" icon="refresh" label="Automationen erneut laden" @click="automations.fetchAutomations().catch(() => {})" />
              <q-item v-else-if="automations.loading && !automations.loaded" dense><q-item-section><q-skeleton type="text" /></q-item-section></q-item>
              <SidebarSessionItem
                v-for="automation in automations.items"
                :key="automation.id"
                :session="automationSession(automation)"
                :now="sidebarNow"
                :title="automation.name"
                :caption="automationStatus(automation).label"
                :status="sessionStatuses.get(automation.executionSessionId)"
                :active="routeSessionId === automation.executionSessionId && route.query.automation === automation.id && controlCenter.open && controlCenter.activeContentId === 'automation'"
                :show-pin="false"
                :draggable="false"
                :menu-open="openAutomationMenuId === automation.id"
                @update:menu-open="openAutomationMenuId = $event ? automation.id : null"
                @open="openAutomation(automation)"
              >
                <template #menu>
                  <AutomationMenuItems :automation="automation" @edit="openAutomation" />
                </template>
              </SidebarSessionItem>
              <q-item v-if="automations.loaded && !automations.items.length" dense class="q-px-sm"><q-item-section><q-item-label caption>Noch keine Automationen</q-item-label></q-item-section></q-item>
            </q-list>
          </section>
          <section aria-labelledby="projects-heading" class="q-mx-xs">
            <SidebarNavItem
              :icon="mdiFolderOutline" icon-color="primary" label="Projekte" label-id="projects-heading" :expanded="projectsSectionOpen"
              aria-controls="project-list" :aria-expanded="projectsSectionOpen"
              @click="projectsSectionOpen = !projectsSectionOpen"
            >
              <template #actions>
                <SidebarIconButton class="sidebar-section__action" icon="add" aria-label="Projekt erstellen" @click="openProjectDialog()" />
              </template>
            </SidebarNavItem>

            <q-list id="project-list" v-show="projectsSectionOpen" class="project-list q-pl-sm">
              <div v-if="projects.loading && !projects.loaded" class="row justify-center q-pa-sm" aria-label="Projekte werden geladen">
                <q-spinner-dots color="primary" size="24px" />
              </div>
              <SidebarNavItem v-else-if="projects.loadFailed && !projects.loaded" icon="refresh" label="Projekte erneut laden" @click="loadProjects" />
              <q-expansion-item
                v-for="project in projects.items"
                :key="project.id"
                v-model="expandedProjects[project.id]"
                :duration="0"
                dense
                hide-expand-icon
                class="project-list-item rounded-borders"
                :class="{
                  'app-surface-tonal': sessionDropTarget === project.id,
                  'project-list-item--menu-open': openProjectActionMenuId === project.id
                }"
                header-class="sidebar-row project-list-item__header rounded-borders q-px-sm q-py-xs"
                @dragover="dragSessionOver($event, project.id)"
                @dragleave="leaveSessionDropTarget"
                @drop="dropSession($event, project.id)"
                @show="loadProjectSessions(project.id)"
              >
                <template #header>
                  <q-item-section side class="q-pr-sm">
                    <q-icon :name="expandedProjects[project.id] ? mdiFolderOpenOutline : mdiFolderOutline" :color="$q.dark.isActive ? 'white' : 'dark'" size="22px" />
                  </q-item-section>
                  <q-item-section>
                    <q-item-label lines="1" class="text-body2" :class="{ 'text-weight-medium': project.hasUnreadMessages }">
                      {{ project.name }}<span v-if="project.hasUnreadMessages" class="app-sr-only">, ungelesene Nachrichten</span>
                    </q-item-label>
                  </q-item-section>
                  <q-item-section side class="project-list-item__actions row no-wrap items-center">
                    <SidebarIconButton
                      icon="add_comment"
                      class="project-list-item__action"
                      :aria-label="`Neue Session in ${project.name}`"
                      @click="openNewSession(project.id)"
                    />
                    <SidebarActionMenu
                      :model-value="openProjectActionMenuId === project.id"
                      class="project-list-item__action"
                      :aria-label="`Aktionen für ${project.name}`"
                      @update:model-value="setProjectActionMenu(project.id, $event)"
                    >
                      <SidebarNavItem icon="edit" label="Projekt bearbeiten" v-close-popup @click="openProjectDialog(project)" />
                      <q-separator />
                      <SidebarNavItem icon="archive" label="Projekt archivieren" v-close-popup @click="archiveProject(project)" />
                      <SidebarNavItem
                        icon="delete_outline" label="Projekt löschen" v-close-popup class="text-negative"
                        @click="deleteProject(project)"
                      />
                    </SidebarActionMenu>
                  </q-item-section>
                </template>
                <SidebarSessionList
                  :list="projectSessionList(project.id)"
                  :now="sidebarNow"
                  :active-session-id="routeSessionId"
                  :statuses="sessionStatuses"
                  :pinning-session-id="pinningSessionId"
                  nested
                  @open="openSession"
                  @rename="openRenameDialog"
                  @move="openMoveDialog"
                  @drag-start="startSessionDrag"
                  @drag-end="endSessionDrag"
                  @delete="removeSession"
                  @archive="archiveSession"
                  @toggle-pin="toggleSessionPinned"
                  @load-more="loadMoreSessions(project.id)"
                  @retry-more="retrySessionPagination(project.id)"
                  @retry="loadProjectSessions(project.id, true)"
                />
              </q-expansion-item>
              <q-item v-if="projects.loaded && !projects.items.length" dense class="q-px-sm">
                <q-item-section><q-item-label caption>Noch keine Projekte</q-item-label></q-item-section>
              </q-item>
            </q-list>
          </section>

          <section
            class="q-mx-xs q-mt-xs"
            :class="{ 'app-surface-tonal': sessionDropTarget === null }"
            aria-labelledby="sessions-heading"
            @dragover="dragSessionOver($event, null)"
            @dragleave="leaveSessionDropTarget"
            @drop="dropSession($event, null)"
          >
            <SidebarNavItem
              icon="chat_bubble_outline" icon-color="primary" label="Sessions" label-id="sessions-heading" :expanded="sessionsSectionOpen"
              aria-controls="standalone-session-list" :aria-expanded="sessionsSectionOpen"
              @click="sessionsSectionOpen = !sessionsSectionOpen"
            >
              <template #actions>
                <SidebarIconButton class="sidebar-section__action" icon="add" aria-label="Neue Session" @click="openNewSession(null)" />
              </template>
            </SidebarNavItem>
            <SidebarSessionList
              id="standalone-session-list"
              v-show="sessionsSectionOpen"
              :list="standaloneSessionList"
              :now="sidebarNow"
              :active-session-id="routeSessionId"
              :statuses="sessionStatuses"
              :pinning-session-id="pinningSessionId"
              show-groups
              @open="openSession"
              @rename="openRenameDialog"
              @move="openMoveDialog"
              @drag-start="startSessionDrag"
              @drag-end="endSessionDrag"
              @delete="removeSession"
              @archive="archiveSession"
              @toggle-pin="toggleSessionPinned"
              @load-more="loadMoreSessions(null)"
              @retry-more="retrySessionPagination(null)"
              @retry="loadStandaloneSessions(true)"
            />
          </section>
        </q-scroll-area>
      </div>

      <div class="drawer-user-area q-pa-xs">
        <SidebarNavItem icon="key" icon-color="primary" label="Variablen &amp; Secrets" :to="{ name: 'personal-variables' }" class="q-mb-xs" @click="closeDrawerOnMobile" />
        <SidebarNavItem icon="archive" icon-color="primary" label="Archiv" :to="{ name: 'archive' }" class="q-mb-xs" @click="closeDrawerOnMobile" />
        <SidebarNavItem
          id="user-menu-trigger" :label="auth.username || 'Benutzer'"
          :aria-label="userMenuLabel" aria-haspopup="menu" role="button"
        >
          <template #icon>
            <q-item-section side class="q-pr-sm"><q-avatar color="primary" text-color="white" size="24px">{{ userInitials }}</q-avatar></q-item-section>
          </template>
          <template #actions><q-icon name="keyboard_arrow_up" size="22px" /></template>

          <AppMenu anchor="top right" self="bottom right" class="app-user-popup">
            <q-list padding>
              <SidebarNavItem v-if="auth.isAdmin" icon="admin_panel_settings" label="Administration" :to="{ name: 'admin' }" v-close-popup @click="closeDrawerOnMobile" />
              <SidebarNavItem icon="tune" label="Einstellungen" to="/settings" v-close-popup @click="closeDrawerOnMobile" />
              <SidebarNavItem icon="manage_accounts" label="Konto" v-close-popup @click="openAccountDialog" />
              <SidebarNavItem :icon="$q.dark.isActive ? 'light_mode' : 'dark_mode'" :label="$q.dark.isActive ? 'Heller Modus' : 'Dunkler Modus'" v-close-popup @click="$q.dark.toggle()" />
              <SidebarNavItem icon="menu_book" label="Dokumentation" href="/docs/" target="_blank" rel="noopener noreferrer" v-close-popup />
              <q-separator class="q-my-sm" />
              <SidebarNavItem icon="logout" label="Abmelden" v-close-popup class="text-negative" @click="logout" />
            </q-list>
          </AppMenu>
        </SidebarNavItem>
      </div>
    </div>
  </q-drawer>

  <AccountDialog
    v-model="accountDialogOpen"
    :before-password-change="confirmDiscardedEdit"
    @closed="focusUserMenuTrigger"
  />
  <SearchDialog @open-project="openSearchProject" @open-session="openSearchSession" />
  <ProjectDialog
    v-if="projectDialogOpen"
    v-model="projectDialogOpen"
    :project="projectDialogTarget"
    @created="onProjectCreated"
  />
  <SessionDialogs
    v-model:rename-open="renameDialogOpen"
    v-model:rename-title="renameTitle"
    v-model:move-open="moveDialogOpen"
    v-model:move-project-id="moveProjectId"
    :renaming="renaming"
    :moving-session="movingSession"
    :move-source-project-id="moveSourceProjectId"
    :project-options="projectOptions"
    :required-rule="requiredRule"
    @rename="renameSession"
    @move="moveSession"
  />
</template>

<script setup>
import { useAppearanceStore } from 'src/features/settings/appearance-store'

import AppMenu from 'src/shared/ui/AppMenu.vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { mdiFolderOpenOutline, mdiFolderOutline } from '@quasar/extras/mdi-v7'
import { isNavigationFailure, NavigationFailureType, useRoute, useRouter } from 'vue-router'
import AccountDialog from 'src/features/auth/AccountDialog.vue'
import AppLogo from 'src/shared/ui/AppLogo.vue'
import SidebarIconButton from 'src/shared/ui/SidebarIconButton.vue'
import SidebarNavItem from 'src/shared/ui/SidebarNavItem.vue'
import ProjectDialog from 'src/features/projects/ProjectDialog.vue'
import SearchDialog from 'src/features/search/SearchDialog.vue'
import SidebarActionMenu from 'src/features/chat/components/SidebarActionMenu.vue'
import AutomationMenuItems from 'src/features/automations/AutomationMenuItems.vue'
import SidebarSessionList from 'src/features/chat/components/SidebarSessionList.vue'
import SidebarSessionItem from 'src/features/chat/components/SidebarSessionItem.vue'
import SessionDialogs from 'src/features/chat/components/SessionDialogs.vue'
import { useProjectNavigation } from 'src/features/projects/useProjectNavigation'
import { useSidebarSessions } from 'src/features/chat/useSidebarSessions'
import { useApprovalStore } from 'stores/approvals'
import { useAutomationStore } from 'stores/automations'
import { useAuthStore } from 'stores/auth'
import { useChatStore } from 'src/features/chat/store'
import { useControlCenterStore } from 'stores/control-center'
import { useInAppNotificationStore } from 'stores/in-app-notifications'
import { useQuestionStore } from 'stores/questions'
import { useSearchStore } from 'stores/search'
import { useProjectStore } from 'stores/projects'
import { useWorkspaceStore } from 'stores/workspace'
import { useConfirm } from 'src/composables/confirm'
import { automationStatus } from 'src/utils/automation-status'

const props = defineProps({ confirmDiscardedEdit: { type: Function, required: true } })
const drawerOpen = defineModel({ type: Boolean, required: true })
const $q = useQuasar()
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const appearance = useAppearanceStore()
const chat = useChatStore()
const controlCenter = useControlCenterStore()
const workspace = useWorkspaceStore()
const approvals = useApprovalStore()
const automations = useAutomationStore()
const notifications = useInAppNotificationStore()
const questions = useQuestionStore()
const search = useSearchStore()
const projects = useProjectStore()
const { confirm } = useConfirm()
const selectedProjectId = computed(() => (
  typeof route.query.project === 'string' ? route.query.project : null
))
const routeSessionId = computed(() => (
  typeof route.query.session === 'string' ? route.query.session : null
))
const sessionsSectionOpen = ref(true)
const automationsSectionOpen = ref(false)
const sidebarNow = ref(Date.now())
let clockTimer
const openAutomationMenuId = ref(null)
const accountDialogOpen = ref(false)
const {
  projectOptions, standaloneSessionList, sessionStatuses,
  renameDialogOpen, renameTitle, renaming, pinningSessionId,
  moveDialogOpen, moveSourceProjectId, moveProjectId, movingSession,
  sessionDropTarget, openRenameDialog, projectSessionList, loadProjectSessions,
  loadStandaloneSessions, startSessionDrag, endSessionDrag, dragSessionOver,
  leaveSessionDropTarget, dropSession, openMoveDialog, moveSession,
  renameSession, toggleSessionPinned, openNewSession, openSession,
  openSearchProject, openSearchSession, loadMoreSessions,
  retrySessionPagination, removeSession, archiveSession
} = useSidebarSessions({
  chat, projects, approvals, questions, workspace, controlCenter, automations,
  route, router, $q, confirm, drawerOpen, selectedProjectId, confirmDiscardedEdit: props.confirmDiscardedEdit
})
const {
  projectsSectionOpen, expandedProjects, openProjectActionMenuId,
  projectDialogOpen, projectDialogTarget,
  loadProjects, openProjectDialog, onProjectCreated,
  setProjectActionMenu, deleteProject
} = useProjectNavigation({
  projects, chat, controlCenter, workspace, automations,
  notifications, router, $q, confirm, selectedProjectId,
  confirmDiscardedEdit: props.confirmDiscardedEdit, openNewSession
})
const userInitials = computed(() => {
  const localPart = (auth.username || '').trim().split('@')[0]
  const parts = localPart.split(/[\s._-]+/).filter(Boolean)
  if (!parts.length) return '?'
  const first = parts[0][0]
  const last = parts.length > 1 ? parts[parts.length - 1][0] : ''
  return `${first}${last}`.toUpperCase()
})
const userMenuLabel = computed(() => 'Benutzermenü öffnen')

function automationSession(automation) {
  const id = automation.executionSessionId
  const session = chat.findCachedSession(id)
  const lastMessageAt = session?.lastMessageAt
  return {
    ...session,
    id,
    lastMessageAt: !lastMessageAt || Date.parse(automation.lastActivityAt) > Date.parse(lastMessageAt)
      ? automation.lastActivityAt : lastMessageAt,
    activeRunStatus: automation.effectiveState?.activeRunStatus
  }
}

function requiredRule(value) {
  return Boolean(String(value || '').trim()) || 'Pflichtfeld'
}

async function openAutomation(automation) {
  const sameSession = routeSessionId.value === automation.executionSessionId
  const projectId = chat.findCachedSession(automation.executionSessionId)?.projectId
  closeDrawerOnMobile()
  try {
    const failure = await router.push({ name: 'chat', query: {
      session: automation.executionSessionId,
      automation: automation.id,
      controlCenter: 'automation',
      ...(projectId ? { project: projectId } : {})
    } })
    if (failure && !isNavigationFailure(failure, NavigationFailureType.duplicated)) return
    if (sameSession && chat.current?.id === automation.executionSessionId) await controlCenter.openApp('automation')
  } catch {
    // The central Axios interceptor displays the error.
  }
}

async function createAutomation() {
  if (!await openNewSession(null)) return
  try {
    await controlCenter.openApp('automation')
  } catch {
    // The central Axios interceptor displays the error.
  }
}

async function archiveProject(project) {
  if (!await props.confirmDiscardedEdit()) return
  try {
    await projects.setArchived(project.id, true)
    await automations.fetchAutomations()
    if (chat.current?.projectId === project.id) await chat.refreshSession(chat.current.id)
    $q.notify({ type: 'positive', message: 'Projekt archiviert' })
  } catch {
    // The Axios interceptor displays the error.
  }
}

function closeDrawerOnMobile() {
  if ($q.screen.lt.md) drawerOpen.value = false
}

function openAccountDialog() {
  closeDrawerOnMobile()
  accountDialogOpen.value = true
}

async function focusUserMenuTrigger() {
  await nextTick()
  document.getElementById(drawerOpen.value ? 'user-menu-trigger' : 'app-navigation-toggle')?.focus()
}

async function closeNavigation() {
  drawerOpen.value = false
  await nextTick()
  document.getElementById('app-navigation-toggle')?.focus()
}

async function logout() {
  if (!await props.confirmDiscardedEdit()) return
  await auth.logout()
}

watch(() => automations.items.some((item) => item.executionSessionId === routeSessionId.value), (selected) => {
  if (selected) automationsSectionOpen.value = true
}, { immediate: true })

watch(selectedProjectId, (projectId, previousProjectId) => {
  if (projectId) {
    projectsSectionOpen.value = true
    expandedProjects[projectId] = true
  } else if (previousProjectId) {
    sessionsSectionOpen.value = true
  }
  void chat.setSessionScope(projectId)
    .then(() => chat.fetchDefaults(projectId))
    .catch(() => {})
}, { immediate: true })

onMounted(async () => {
  clockTimer = window.setInterval(() => {
    if (document.visibilityState === 'visible') sidebarNow.value = Date.now()
  }, 60_000)
  await Promise.allSettled([loadProjects(), loadStandaloneSessions()])
})
onBeforeUnmount(() => window.clearInterval(clockTimer))
</script>
