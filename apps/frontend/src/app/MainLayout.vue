<template>
  <q-layout
    view="hHh Lpr fFf"
    class="app-shell"
    :class="{
      'app-shell--chat': route.name === 'chat',
      'app-shell--navigation-overlay': showFloatingNavigation,
      'app-shell--floating-chat-controls': route.name === 'chat' && showFloatingControls
    }"
    :style="visualViewportStyle"
  >
    <AppNavigation v-model="drawerOpen" :confirm-discarded-edit="confirmDiscardedEdit" />

    <div v-if="showFloatingControls" class="app-floating-controls" aria-label="Schnellaktionen">
      <q-btn
        v-if="showFloatingNavigation"
        id="app-navigation-toggle"
        class="app-floating-control app-floating-control--flat"
        round
        flat
        icon="menu"
        aria-label="Navigation öffnen"
        aria-controls="app-navigation-drawer"
        :aria-expanded="drawerOpen"
        @click="openNavigation"
      />
      <q-space />
      <q-btn
        v-if="showFloatingControlCenter"
        id="control-center-toggle"
        class="app-floating-control"
        round
        unelevated
        icon="apps"
        :aria-label="controlCenterLabel"
        :aria-expanded="controlCenterVisible"
        aria-controls="control-center-panel"
        @click="openControlCenter"
      >
        <q-badge
          v-if="notifications.unreadCount"
          floating
          rounded
          color="negative"
          :label="notifications.unreadCount > 99 ? '99+' : notifications.unreadCount"
          aria-hidden="true"
        />
      </q-btn>
    </div>

    <q-dialog
      :transition-duration="0"
      v-if="route.name === 'chat' && $q.screen.lt.md"
      :model-value="controlCenterVisible"
      aria-label="Control Center"
      maximized
      @update:model-value="setControlCenterOpen"
    >
      <q-card class="app-file-dialog">
        <ControlCenterPanel />
      </q-card>
    </q-dialog>

    <q-page-container>
      <router-view />
    </q-page-container>
  </q-layout>
</template>

<script setup>
import { computed, provide, ref } from 'vue'
import { useQuasar } from 'quasar'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from 'vue-router'
import AppNavigation from 'src/app/AppNavigation.vue'
import ControlCenterPanel from 'src/app/control-center/ControlCenterPanel.vue'
import { useShellSync } from 'src/app/useShellSync'
import { useAutomationStore } from 'stores/automations'
import { useChatStore } from 'src/features/chat/store'
import { useControlCenterStore } from 'stores/control-center'
import { useInAppNotificationStore } from 'stores/in-app-notifications'
import { useWorkspaceStore } from 'stores/workspace'
import { useConfirm } from 'src/composables/confirm'

defineOptions({ name: 'MainLayout' })

const $q = useQuasar()
const drawerOpen = ref(!$q.screen.lt.md)
const route = useRoute()
const chat = useChatStore()
const controlCenter = useControlCenterStore()
const workspace = useWorkspaceStore()
const { confirm } = useConfirm()
const automations = useAutomationStore()
const notifications = useInAppNotificationStore()
const showFloatingNavigation = computed(() => !drawerOpen.value)
const controlCenterVisible = computed(() => (
  controlCenter.open && (!$q.screen.lt.md || !controlCenter.restoredOpen)
))
const showFloatingControlCenter = computed(() => route.name === 'chat' && !controlCenterVisible.value)
const showFloatingControls = computed(() => (
  showFloatingNavigation.value || showFloatingControlCenter.value
))
const controlCenterLabel = computed(() => notifications.unreadCount
  ? `Benachrichtigungen im Control Center öffnen, ${notifications.unreadCount} ungelesen`
  : 'Control Center öffnen')
const { visualViewportStyle } = useShellSync({ drawerOpen, controlCenterVisible })
provide('confirmDiscardedEdit', confirmDiscardedEdit)

function openNavigation() {
  drawerOpen.value = true
}

async function confirmDiscardedEdit({ discard = true } = {}) {
  if (!workspace.hasDirtyEditors && !automations.dirty) return true
  const accepted = await confirm(
    'Sollen die ungespeicherten Änderungen verworfen werden?',
    'Änderungen verwerfen?',
    { okLabel: 'Verwerfen' }
  )
  if (accepted && discard) {
    workspace.discardAllEdits()
    automations.dirty = false
  }
  return accepted
}

async function openControlCenter() {
  try {
    await controlCenter.openApp(notifications.unreadCount ? 'notifications' : controlCenter.activeContentId || 'files')
  } catch {
    // The central API interceptor displays the error and restores the previous tabs.
  }
}

async function setControlCenterOpen(open) {
  try {
    await controlCenter.setOpen(open)
  } catch {
    // The central API interceptor displays the error and restores the previous state.
  }
}

onBeforeRouteUpdate((to, from) => {
  if (to.name === 'chat' && from.name === 'chat'
    && to.query.session === from.query.session && to.query.project === from.query.project) return true
  if (to.name === 'chat' && from.name === 'chat'
    && to.query.session === from.query.session
    && (to.query.subagent !== from.query.subagent
      || (to.query.project !== from.query.project
        && to.query.session === chat.current?.id
        && !controlCenter.fileTabs.some((tab) => (
          tab.fileTarget.scope === 'session'
          && tab.fileTarget.rootIdentity !== chat.current?.id
          && workspace.fileTabDirty(tab.tabId)
        ))))) return true
  return confirmDiscardedEdit()
})
onBeforeRouteLeave(confirmDiscardedEdit)
</script>
