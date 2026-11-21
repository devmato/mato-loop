<template>
  <div v-if="isAppleStandalone" class="pwa-status-bar-background" aria-hidden="true" />
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

  <q-banner
    v-if="updateAvailable"
    dense
    rounded
    class="pwa-update-banner app-surface shadow-2"
    role="status"
  >
    Eine neue Version der Weboberfläche ist bereit.
    <div>
      <q-checkbox v-model="showChanges" dense class="q-mt-sm" label="Neuerungen nach dem Neuladen anzeigen" />
    </div>
    <template #action>
      <q-btn flat dense no-caps color="primary" label="Später" @click="snoozeUpdate" />
      <q-btn flat dense no-caps color="primary" label="Jetzt neu laden" :loading="restarting" @click="applyUpdate" />
    </template>
  </q-banner>
</template>

<script setup>
import { Platform } from 'quasar'
import { defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useChangelogStore } from 'src/features/changelog/store'
import AppLogo from 'src/shared/ui/AppLogo.vue'
import { useAppearanceStore } from 'src/features/settings/appearance-store'
import { useAuthStore } from 'stores/auth'
import { notifyError } from 'src/utils/notifications'
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
const isAppleStandalone = navigator.standalone === true
  || (Platform.is.ios && window.matchMedia('(display-mode: standalone)').matches)
const ChangelogDialog = defineAsyncComponent(() => import('src/features/changelog/ChangelogDialog.vue'))
const showChanges = ref(true)
const startupSlow = ref(false)
const updateAvailable = ref(false)
const restarting = ref(false)
let startupTimer = null
let updateTimer = null

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
  startupTimer = window.setTimeout(() => { startupSlow.value = true }, 3000)
  window.addEventListener(PWA_UPDATE_EVENT, showUpdate)
  void detectWaitingUpdate()
})

onBeforeUnmount(() => {
  window.clearTimeout(startupTimer)
  window.clearTimeout(updateTimer)
  window.removeEventListener(PWA_UPDATE_EVENT, showUpdate)
})
</script>

<style scoped>
/* WebKit samples the color of fixed bands taller than 10px. Clip the paint so the band does not cover the tabs. */
.pwa-status-bar-background {
  position: fixed;
  z-index: 3001;
  inset: 0 0 auto;
  height: 11px;
  background-color: var(--app-shell-bg);
  background-clip: text;
  pointer-events: none;
}

.startup-view {
  min-height: 100dvh;
  background: var(--app-shell-bg);
  color: var(--app-text);
}

.pwa-update-banner {
  position: fixed;
  z-index: 7000;
  right: max(16px, env(safe-area-inset-right));
  bottom: max(16px, env(safe-area-inset-bottom));
  left: max(16px, env(safe-area-inset-left));
  max-width: 560px;
  margin-left: auto;
}
</style>
