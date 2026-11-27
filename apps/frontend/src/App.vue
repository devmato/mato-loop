<template>
  <Teleport to="body">
    <div ref="noticeArea" class="app-notices">
      <AppNotice v-if="backendUnavailable && !outageDismissed" icon="wifi_off" tone="warning" title="Verbindung unterbrochen" @dismiss="outageDismissed = true">
        MatoLoop prüft die Verbindung automatisch.
      </AppNotice>
      <AppNotice v-if="updateAvailable" icon="system_update" tone="info" title="Eine neue Version der Weboberfläche ist bereit." :dismiss-disabled="restarting" @dismiss="snoozeUpdate">
        <q-checkbox v-model="showChanges" dense label="Neuerungen nach dem Neuladen anzeigen" />
        <template #actions>
          <q-btn flat dense no-caps color="primary" label="Später" @click="snoozeUpdate" />
          <q-btn flat dense no-caps color="primary" label="Jetzt neu laden" :loading="restarting" @click="applyUpdate" />
        </template>
      </AppNotice>
    </div>
  </Teleport>
  <div v-if="!auth.statusLoaded" class="startup-view column items-center justify-center">
    <AppLogo :size="56" />
    <div class="text-h6 text-weight-bold q-mt-md">{{ appearance.appName }}</div>
    <q-spinner-dots color="primary" size="32px" class="q-mt-sm" />
    <q-btn
      v-if="startupSlow"
      flat
      no-caps
      color="primary"
      label="Erneut versuchen"
      class="q-mt-md"
      @click="reload"
    />
  </div>
  <router-view v-else />
  <ChangelogDialog v-if="auth.authenticated && changelog.open" />
</template>

<script setup>
import { defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppNotice from 'src/shared/ui/AppNotice.vue'
import { useChangelogStore } from 'src/features/changelog/store'
import AppLogo from 'src/shared/ui/AppLogo.vue'
import { useAppearanceStore } from 'src/features/settings/appearance-store'
import { useAuthStore } from 'stores/auth'
import { backendUnavailable, notifyError } from 'src/utils/notifications'
import {
  PWA_UPDATE_EVENT,
  consumePwaChangelog,
  pwaUpdateSnoozeRemaining,
  requestPwaUpdate,
  snoozePwaUpdate
} from 'src/app/pwa-update'

defineOptions({ name: 'App' })

const auth = useAuthStore()
const appearance = useAppearanceStore()
const changelog = useChangelogStore()
const route = useRoute()
const ChangelogDialog = defineAsyncComponent(() => import('src/features/changelog/ChangelogDialog.vue'))
const noticeArea = ref(null)
const showChanges = ref(true)
const startupSlow = ref(false)
const updateAvailable = ref(false)
const outageDismissed = ref(false)
const restarting = ref(false)
let startupTimer = null
let updateTimer = null

watch(backendUnavailable, (unavailable) => {
  if (!unavailable) outageDismissed.value = false
})

function reload() {
  window.location.reload()
}

function showUpdate() {
  window.clearTimeout(updateTimer)
  const snoozeRemaining = pwaUpdateSnoozeRemaining()
  if (snoozeRemaining) {
    updateTimer = window.setTimeout(() => { void detectWaitingUpdate() }, snoozeRemaining)
    return
  }
  updateAvailable.value = true
}

function snoozeUpdate() {
  snoozePwaUpdate()
  updateAvailable.value = false
  showUpdate()
}

async function detectWaitingUpdate() {
  try {
    const registration = await navigator.serviceWorker?.getRegistration?.()
    if (registration?.waiting) showUpdate()
    else updateAvailable.value = false
  } catch {
    // Keep the current prompt; the next check can reconcile it.
  }
}

async function applyUpdate() {
  if (restarting.value) return
  restarting.value = true
  try {
    if (!await requestPwaUpdate({ showChanges: showChanges.value, userId: auth.userId })) await detectWaitingUpdate()
  } catch (error) {
    notifyError(error, 'Die Aktualisierung konnte nicht abgeschlossen werden. Versuche es erneut.')
    await detectWaitingUpdate()
  } finally {
    restarting.value = false
  }
}

// Quasar dismisses open dialogs on navigation, including the initial route.
watch(() => route.matched.length > 0 && auth.authenticated && auth.userId, (userId) => {
  if (!userId) return
  const comparison = consumePwaChangelog(userId)
  if (comparison) void changelog.show(comparison)
}, { immediate: true })

onMounted(() => {
  // Keep Quasar's notification lifecycle while sharing the overlay with app notices.
  const notifyHost = document.getElementById('q-notify')
  if (notifyHost) noticeArea.value.append(notifyHost)
  startupTimer = window.setTimeout(() => { startupSlow.value = true }, 3000)
  window.addEventListener(PWA_UPDATE_EVENT, showUpdate)
  void detectWaitingUpdate()
})

onBeforeUnmount(() => {
  const notifyHost = noticeArea.value?.querySelector('#q-notify')
  if (notifyHost) document.body.append(notifyHost)
  window.clearTimeout(startupTimer)
  window.clearTimeout(updateTimer)
  window.removeEventListener(PWA_UPDATE_EVENT, showUpdate)
})
</script>

<style scoped>
.startup-view {
  min-height: 100dvh;
  background: var(--app-shell-bg);
  color: var(--app-text);
}

</style>
