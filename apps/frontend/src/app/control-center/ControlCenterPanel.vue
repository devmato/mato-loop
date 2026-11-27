<template>
  <section id="control-center-panel" class="control-center column full-height no-wrap" aria-label="Control Center">
    <div class="control-center__tabs row items-center no-wrap q-px-sm" :class="{ 'control-center__tabs--desktop': maximizable }">
      <slot name="navigation" />
      <q-tabs
        :model-value="activeFlexibleTabId"
        class="control-center__tab-strip col"
        content-class="app-tab-scroll control-center__tab-scroll"
        dense
        no-caps
        inline-label
        align="left"
        :breakpoint="0"
        indicator-color="transparent"
        @update:model-value="activate"
      >
        <AppClosableTab
          v-for="tab in flexibleTabs"
          :key="tab.tabId"
          :tab-id="`control-center-tab-${tab.tabId}`"
          :name="tab.tabId"
          :label="tabLabel(tab)"
          :icon="content(tab.appId).icon"
          :alert-icon="tab.appId === 'terminal' ? 'priority_high' : undefined"
          :active="activeFlexibleTabId === tab.tabId"
          :disable="controlCenter.saving || !controlCenter.ready"
          :close-tooltip="tab.kind === 'file' ? 'Datei schließen' : 'App aus Session entfernen'"
          class="control-center__tab-shell"
          :class="{
            'control-center__tab--dragging': draggedIntegrationId === tab.tabId,
            'control-center__tab--drop-before': dropTarget?.id === tab.tabId && !dropTarget.after,
            'control-center__tab--drop-after': dropTarget?.id === tab.tabId && dropTarget.after
          }"
          :draggable="controlCenter.ready && !controlCenter.saving"
          @dragstart="startDrag($event, tab.tabId)"
          @dragover="updateDropTarget($event, tab.tabId)"
          @drop="dropIntegration"
          @dragend="endDrag"
          @close="closeTab(tab)"
        />

        <q-btn
          v-if="launcherIntegrations.length"
          flat
          square
          dense
          icon="add"
          class="control-center__tab-add self-stretch"
          aria-label="App hinzufügen"
          :disable="controlCenter.saving || !controlCenter.ready || (!chat.current && chat.activeQueuedMessages.length > 0)"
        >
          <q-tooltip>App hinzufügen</q-tooltip>
          <AppMenu anchor="bottom right" self="top right">
            <q-list padding>
              <q-item
                v-for="item in launcherIntegrations"
                :key="item.id"
                clickable
                v-ripple
                v-close-popup
                @click="openContent(item.id)"
              >
                <q-item-section avatar><q-icon :name="item.icon" /></q-item-section>
                <q-item-section>{{ item.label }}</q-item-section>
              </q-item>
            </q-list>
          </AppMenu>
        </q-btn>
      </q-tabs>

      <q-btn
        v-if="$q.screen.gt.sm && flexibleTabs.length"
        flat
        round
        size="16px"
        class="control-center__tab-menu"
        icon="more_vert"
        aria-label="Geöffnete Tabs anzeigen"
      >
        <q-tooltip>Geöffnete Tabs</q-tooltip>
        <AppMenu anchor="bottom right" self="top right">
          <q-list padding>
            <q-item
              v-for="tab in flexibleTabs"
              :key="tab.tabId"
              clickable
              v-ripple
              v-close-popup
              :active="activeFlexibleTabId === tab.tabId"
              :disable="controlCenter.saving || !controlCenter.ready"
              @click="activate(tab.tabId)"
            >
              <q-item-section avatar><q-icon :name="content(tab.appId).icon" /></q-item-section>
              <q-item-section>{{ tabLabel(tab) }}</q-item-section>
              <q-item-section v-if="activeFlexibleTabId === tab.tabId" side><q-icon name="check" color="primary" /></q-item-section>
            </q-item>
          </q-list>
        </AppMenu>
      </q-btn>

      <q-btn
        v-if="$q.screen.lt.md && chat.running"
        flat
        round
        dense
        icon="stop"
        color="negative"
        aria-label="Agent-Anfrage stoppen"
        :disable="chat.stopping"
        @click="chat.stop()"
      />

      <q-btn
        id="control-center-session-button"
        flat
        round
        size="16px"
        class="q-ml-xs"
        icon="tune"
        :color="!controlCenter.activeGlobalSectionId && controlCenter.activeAppId === 'session' ? 'primary' : undefined"
        :loading="!controlCenter.ready && controlCenter.requestedAppId === 'session'"
        aria-label="Session öffnen"
        @click="openContent('session')"
      >
        <q-tooltip>Session</q-tooltip>
      </q-btn>

      <q-btn
        v-for="item in controlCenterGlobalSections"
        :key="item.id"
        flat
        round
        size="16px"
        class="q-ml-xs"
        :icon="item.icon"
        :color="controlCenter.activeGlobalSectionId === item.id ? 'primary' : undefined"
        :loading="!controlCenter.ready && controlCenter.requestedAppId === item.id"
        :aria-label="`${item.label} öffnen, ${notifications.unreadCount} ungelesen`"
        @click="openContent(item.id)"
      >
        <q-badge
          v-if="badge(item.id)"
          floating
          rounded
          color="negative"
          text-color="white"
          :label="badge(item.id)"
        />
        <q-tooltip>{{ item.label }}</q-tooltip>
      </q-btn>

      <q-btn
        v-if="maximizable"
        flat
        round
        dense
        size="20px"
        class="control-center__resize q-ml-sm"
        :icon="maximized ? 'fullscreen_exit' : 'fullscreen'"
        :aria-label="maximizeLabel"
        @click="emit('toggle-maximized')"
      >
        <q-tooltip>{{ maximizeLabel }}</q-tooltip>
      </q-btn>

      <q-btn
        flat
        round
        dense
        size="20px"
        class="control-center__close q-ml-sm app-surface-tonal"
        icon="close"
        aria-label="Control Center schließen"
        @click="closePanel"
      >
        <q-tooltip>Control Center schließen</q-tooltip>
      </q-btn>
    </div>
    <q-separator />

    <div class="control-center__content col relative-position">
      <component
        :is="content(tab.appId).component"
        v-for="tab in mountedAppTabs"
        v-show="controlCenter.activeContentId === tab.tabId"
        :key="tab.tabId"
        class="fit"
      />
      <component
        :is="content(controlCenter.activeTab.appId).component"
        v-if="controlCenter.activeTab?.kind === 'app' && ['files', 'computer-files'].includes(controlCenter.activeTab.appId)"
        v-show="!controlCenter.activeGlobalSectionId"
        :key="controlCenter.activeTab.appId"
        class="fit"
      />
      <component
        :is="content(controlCenter.activeTab.appId).component"
        v-if="controlCenter.activeTab?.kind === 'file'"
        v-show="!controlCenter.activeGlobalSectionId"
        :key="`${controlCenter.activeTab.tabId}:${controlCenter.activeTab.fileTarget.path}`"
        :tab="controlCenter.activeTab"
        class="fit"
      />
      <component
        :is="item.component"
        v-for="item in mountedGlobalSections"
        v-show="controlCenter.activeContentId === item.id"
        :key="item.id"
        class="fit"
      />
      <div v-if="!controlCenter.activeContentId" class="fit column no-wrap">
        <AppStateMessage
          class="col q-pa-xl"
          icon="extension_off"
          title="Keine App in dieser Session"
          description="Füge eine App hinzu, um sie hier zu verwenden."
        >
          <template #actions>
            <q-btn
              unelevated
              no-caps
              color="primary"
              icon="add"
              label="App hinzufügen"
              :disable="!launcherIntegrations.length || !controlCenter.ready || (!chat.current && chat.activeQueuedMessages.length > 0)"
            >
              <AppMenu anchor="bottom middle" self="top middle">
                <q-list padding>
                  <q-item v-for="item in launcherIntegrations" :key="item.id" clickable v-ripple v-close-popup @click="openContent(item.id)">
                    <q-item-section avatar><q-icon :name="item.icon" /></q-item-section>
                    <q-item-section>{{ item.label }}</q-item-section>
                  </q-item>
                </q-list>
              </AppMenu>
            </q-btn>
          </template>
        </AppStateMessage>
      </div>
    </div>
    <template v-if="activeFooter">
      <q-separator />
      <footer class="control-center__footer" aria-label="App-Informationen und Steuerelemente">
        <component :is="activeFooter" class="full-width" />
      </footer>
    </template>
  </section>
</template>

<script setup>
import AppMenu from 'src/shared/ui/AppMenu.vue'
import { agentDisplayName } from 'src/features/computers/display'
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useTabReorder } from 'src/shared/ui/useTabReorder'
import AppClosableTab from 'src/shared/ui/AppClosableTab.vue'
import AppStateMessage from 'src/shared/ui/AppStateMessage.vue'
import { useControlCenterStore } from 'stores/control-center'
import { useExecutionAgentStore } from 'stores/execution-agents'
import { useInAppNotificationStore } from 'stores/in-app-notifications'
import { useMailStore } from 'stores/mail'
import { useChatStore } from 'src/features/chat/store'
import { useBrowserStore } from 'stores/browser'
import { useProjectStore } from 'stores/projects'
import { useIntegrationStore } from 'stores/integrations'
import { useWorkspaceStore } from 'stores/workspace'
import { useConfirm } from 'src/composables/confirm'
import {
  controlCenterContentsById,
  controlCenterGlobalSections,
  controlCenterIntegrations
} from 'src/app/control-center/apps'
import {
  codingControlCenterAvailable,
  browserControlCenterAvailable,
  computerFilesControlCenterAvailable,
  gitControlCenterAvailable,
  mailControlCenterAvailable,
  tbsDeskControlCenterAvailable,
  terminalControlCenterState
} from 'src/app/control-center/manifest'

defineOptions({ name: 'ControlCenterPanel' })

const props = defineProps({ maximizable: Boolean, maximized: Boolean })
const emit = defineEmits(['close', 'toggle-maximized'])
const maximizeLabel = computed(() => props.maximized
  ? 'Geteilte Ansicht wiederherstellen'
  : 'Control Center maximieren')
const { confirm } = useConfirm()
const controlCenter = useControlCenterStore()
const notifications = useInAppNotificationStore()
const mail = useMailStore()
const browser = useBrowserStore()
const chat = useChatStore()
const projects = useProjectStore()
const executionAgents = useExecutionAgentStore()
const integrations = useIntegrationStore()
const workspace = useWorkspaceStore()
const { draggedId: draggedIntegrationId, dropTarget, startDrag, updateDropTarget, drop: dropIntegration, endDrag } = useTabReorder(
  (source, target, after) => controlCenter.reorderIntegration(source, target, after)
)
const flexibleTabs = computed(() => controlCenter.tabs.filter((tab) => tab.appId !== 'session'))
// Keep state in apps that were already visited without mounting every saved tab.
const mountedContentIds = ref(new Set())
watch(() => controlCenter.activeContentId, id => {
  if (id) mountedContentIds.value.add(id)
}, { immediate: true })
const mountedAppTabs = computed(() => controlCenter.tabs.filter(tab => (
  tab.kind === 'app' && !['files', 'computer-files'].includes(tab.appId) && mountedContentIds.value.has(tab.tabId)
)))
const mountedGlobalSections = computed(() => controlCenterGlobalSections.filter(item => mountedContentIds.value.has(item.id)))
const activeFlexibleTabId = computed(() => (
  controlCenter.activeGlobalSectionId || controlCenter.activeAppId === 'session'
    ? null
    : controlCenter.activeAppId
))
const activeFooter = computed(() => {
  const contentId = controlCenter.activeGlobalSectionId || controlCenter.activeTab?.appId
  return contentId === 'browser' && !browser.ready ? null : content(contentId)?.footerComponent
})
const closedIntegrations = computed(() => controlCenterIntegrations.filter(
  (item) => !item.fixed && !item.resource && !controlCenter.tabs.some((tab) => tab.appId === item.id)
))
onMounted(() => {
  if (closedIntegrations.value.some(item => item.id === 'mail') && !mail.accountsLoaded && !mail.accountsLoading) {
    void mail.fetchAccounts().catch(() => {})
  }
})
const activeProject = computed(() => projects.items.find(
  (project) => project.id === (chat.current ? chat.current.projectId : chat.sessionsProjectId)
))
const launcherIntegrations = computed(() => closedIntegrations.value.filter((item) => (
  (item.id !== 'mail' || mailControlCenterAvailable(mail))
  && (item.id !== 'tbs-desk' || tbsDeskControlCenterAvailable(integrations))
  && (item.id !== 'coding' || codingControlCenterAvailable(activeProject.value))
   && (item.id !== 'computer-files' || computerFilesControlCenterAvailable(chat.activeContext, executionAgents))
   && (item.id !== 'git' || gitControlCenterAvailable(chat.activeContext, executionAgents))
  && (item.id !== 'terminal' || terminalControlCenterState(chat.activeContext, executionAgents).available)
  && (item.id !== 'browser' || browserControlCenterAvailable(chat.activeContext, executionAgents, integrations))
)))
function content(contentId) {
  return controlCenterContentsById.get(contentId)
}

function tabLabel(tab) {
  if (tab.kind === 'file') return tab.fileTarget.name
    + (tab.fileTarget.revision != null ? ` · Version ${tab.fileTarget.revision}` : '')
  if (['terminal', 'computer-files', 'browser'].includes(tab.appId)) {
    const agent = executionAgents.byId(chat.activeContext?.preferredExecutionAgentId)
    return agent ? `${content(tab.appId).label} · ${agentDisplayName(agent)}` : content(tab.appId).label
  }
  return content(tab.appId).label
}

function badge(appId) {
  const count = appId === 'notifications'
    ? notifications.unreadCount
    : 0
  return count > 99 ? '99+' : count || null
}

async function closePanel() {
  try {
    await controlCenter.setOpen(false)
  } catch {
    // The central API interceptor displays the error and restores the open panel.
    return
  }
  emit('close')
  await nextTick()
  document.getElementById('control-center-toggle')?.focus()
}

async function removeIntegration(appId) {
  try {
    await controlCenter.removeIntegration(appId)
    // Terminal cleanup follows persisted tabs on the backend, never optimistic browser state.
  } catch {
    // The central API interceptor displays the error and the store restores server state.
  }
}

async function closeTab(tab) {
  if (tab.kind === 'file') {
    if (workspace.fileTabDirty(tab.tabId) && !await confirm(
      'Sollen die ungespeicherten Änderungen verworfen werden?',
      'Änderungen verwerfen?',
      { okLabel: 'Verwerfen' }
    )) return
    try {
      await controlCenter.closeFile(tab.tabId)
      workspace.discardFileTab(tab.tabId)
    } catch {
      // The central API interceptor displays the error and keeps the draft available.
      return
    }
  } else {
    await removeIntegration(tab.appId)
  }
  await nextTick()
  const activeId = controlCenter.activeAppId === 'session'
    ? 'control-center-session-button'
    : `control-center-tab-${controlCenter.activeAppId}`
  document.getElementById(activeId)?.focus()
}

async function activate(appId) {
  try {
    await controlCenter.activate(appId)
  } catch {
    // The central API interceptor displays the error and the store restores server state.
  }
}

async function openContent(contentId) {
  try {
    await controlCenter.openApp(contentId)
  } catch {
    // The central API interceptor displays the error and the store restores server state.
  }
}

</script>
