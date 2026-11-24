<template>
  <router-view v-if="['settings-mcp-tools', 'admin-mcp-tools'].includes(route.name)" />
  <section v-else>
    <header class="settings-section-header">
      <div>
        <h2 class="text-h6 text-weight-bold q-my-none">MCP-Server</h2>
        <p class="text-body2 app-muted q-mb-none q-mt-xs">Externe Tools über das Model Context Protocol bereitstellen.</p>
      </div>
      <div v-if="showManagement" class="settings-scope-actions row items-center col-auto">
        <q-btn color="primary" unelevated no-caps no-wrap icon="add" label="MCP-Server hinzufügen" :disable="servers.loading || (personalUnavailable && !auth.isAdmin)" @click="openCreate" />
      </div>
    </header>

    <q-tabs v-if="admin" v-model="section" dense no-caps align="left" active-color="primary" indicator-color="primary" class="q-mb-lg">
      <q-tab name="general" icon="settings" label="Einstellungen" />
      <q-tab name="management" icon="hub" label="Verwaltung" />
    </q-tabs>
    <q-banner v-if="showManagement" rounded class="app-surface-tonal q-mb-md">
      <template #avatar><q-icon name="security" color="primary" /></template>
       MCP-Tools können externe Aktionen ausführen. Berechtigungen werden beim jeweiligen Server verwaltet.
    </q-banner>
    <PersonalSettingsGate v-if="admin && section === 'general'" :model-value="servers.personalAllowed" title="Persönliche MCP-Server erlauben" description="Benutzer können eigene MCP-Server einrichten." :disabled="servers.settingsLoading || servers.settingsSaving || servers.settingsLoadFailed" @update:model-value="saveSettings" />
    <q-banner v-if="personalBlocked" rounded class="app-surface-tonal q-mb-md">
      <template #avatar><q-icon name="block" color="warning" /></template>
      Persönliche MCP-Server sind für diese Instanz deaktiviert.
    </q-banner>
    <q-banner v-if="servers.settingsLoadFailed" rounded class="app-surface-tonal q-mb-md">
      Die MCP-Einstellungen konnten nicht geladen werden.
      <template #action><q-btn flat no-caps color="primary" label="Erneut laden" @click="loadPage" /></template>
    </q-banner>
    <q-banner v-if="servers.loadFailed" rounded class="app-surface-tonal q-mb-md">
      Die MCP-Server konnten nicht geladen werden.
      <template #action><q-btn flat no-caps color="primary" label="Erneut laden" @click="load" /></template>
    </q-banner>
    <q-banner v-if="servers.oauthFinishing" rounded class="app-surface-tonal q-mb-md" role="status">
      <template #avatar><q-spinner color="primary" /></template>
      OAuth-Anmeldung wird abgeschlossen und die Tools werden geprüft.
    </q-banner>

    <q-card v-if="showManagement" flat class="app-surface">
      <q-list separator>
         <q-item v-if="servers.loaded && !visibleServers.length" class="q-py-lg">
          <q-item-section avatar><q-avatar color="grey-7" text-color="white" icon="hub" /></q-item-section>
          <q-item-section>
            <q-item-label>Noch kein MCP-Server konfiguriert</q-item-label>
            <q-item-label caption>Verbinde einen HTTP-Endpunkt oder ein Programm auf deinem Computer.</q-item-label>
          </q-item-section>
        </q-item>
         <q-item v-for="server in visibleServers" :key="server.id" class="settings-list-item q-py-md">
          <q-item-section avatar top><q-avatar color="primary" text-color="white" icon="hub" /></q-item-section>
          <q-item-section>
            <div class="row items-center q-gutter-sm">
              <q-item-label class="settings-list-name text-weight-medium">{{ server.name }}</q-item-label>
              <q-badge outline color="grey-7" :label="server.scope === 'instance' ? 'Global' : 'Persönlich'" />
              <q-badge outline color="primary" :label="server.transport === 'stdio' ? 'Auf Computer (stdio)' : 'HTTP'" />
              <q-chip dense :color="server.enabled ? 'positive' : 'grey-7'" text-color="white">{{ server.enabled ? 'Aktiv' : 'Deaktiviert' }}</q-chip>
              <q-chip v-if="server.deletedComputer" dense outline color="warning">Computer entfernt</q-chip>
              <q-chip v-else-if="server.available === false" dense outline color="warning">Nicht verfügbar</q-chip>
              <q-chip v-if="server.status && server.status !== 'ready'" dense outline color="warning">{{ statusLabel(server.status) }}</q-chip>
              <q-chip v-if="server.oauth" dense outline :color="server.authStatus === 'authorized' ? 'positive' : 'warning'">{{ server.authStatus === 'authorized' ? 'OAuth angemeldet' : server.bootstrapRequired ? 'Admin-Einrichtung erforderlich' : 'Anmeldung erforderlich' }}</q-chip>
            </div>
            <q-item-label caption lines="1">{{ server.transport === 'stdio' ? computerName(server) : server.endpoint }}</q-item-label>
            <q-item-label caption>{{ server.tools?.length || server.toolCount || 0 }} erkannte Tools</q-item-label>
          </q-item-section>
          <q-item-section side top class="settings-list-actions">
            <div class="row items-center no-wrap q-gutter-sm">
              <q-toggle dense :model-value="server.enabled" :aria-label="`${server.name} ${server.enabled ? 'deaktivieren' : 'aktivieren'}`" :disable="!canEdit(server) || togglingId !== null" @update:model-value="toggleServer(server, $event)" />
              <q-btn v-if="canConnect(server) && server.authStatus !== 'authorized'" outline no-caps color="primary" icon="login" label="Mit Konto verbinden" @click="connectServer(server)" />
              <q-btn v-else-if="canConnect(server)" flat no-caps color="negative" icon="logout" label="Abmelden" @click="disconnectServer(server)" />
              <div class="row items-center no-wrap q-gutter-xs">
                <q-btn flat round dense icon="construction" :aria-label="`${server.name}: Tools und Berechtigungen anzeigen`" :disable="!canEdit(server)" :to="{ name: admin ? 'admin-mcp-tools' : 'settings-mcp-tools', params: { serverId: server.id } }" />
                <q-btn flat round dense icon="more_vert" :aria-label="`Aktionen für ${server.name}`" :loading="refreshingId === server.id || removingId === server.id">
                  <AppMenu auto-close>
                    <q-list>
                      <q-item v-if="server.transport === 'stdio'" clickable :disable="!canManageSessions(server)" @click="openSessions(server)">
                        <q-item-section avatar><q-icon name="terminal" /></q-item-section>
                        <q-item-section>Aktive Sitzungen</q-item-section>
                      </q-item>
                      <q-item clickable :disable="refreshingId !== null || !canEdit(server)" @click="refreshServer(server)">
                        <q-item-section avatar><q-icon name="refresh" /></q-item-section>
                        <q-item-section>Tools aktualisieren</q-item-section>
                      </q-item>
                      <q-item clickable :disable="!canEdit(server)" @click="openEdit(server)">
                        <q-item-section avatar><q-icon name="edit" /></q-item-section>
                        <q-item-section>Bearbeiten</q-item-section>
                      </q-item>
                      <q-item clickable :disable="removingId !== null || !canRemove(server)" @click="removeServer(server)">
                        <q-item-section avatar><q-icon name="delete" color="negative" /></q-item-section>
                        <q-item-section class="text-negative">Löschen</q-item-section>
                      </q-item>
                    </q-list>
                  </AppMenu>
                </q-btn>
              </div>
            </div>
          </q-item-section>
        </q-item>
      </q-list>
      <q-inner-loading :showing="servers.loading" />
    </q-card>
    <McpServerDialog v-model="dialogOpen" :server="selectedServer" @update:model-value="onDialogChange" />
    <McpSessionsDialog v-model="sessionsOpen" :server="sessionsServer" />
  </section>
</template>

<script setup>
import AppMenu from 'src/shared/ui/AppMenu.vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useQuasar } from 'quasar'
import { useRoute, useRouter } from 'vue-router'
import { notifyError } from 'src/utils/notifications'
import McpServerDialog from 'src/features/mcp/McpServerDialog.vue'
import McpSessionsDialog from 'src/features/mcp/McpSessionsDialog.vue'
import { useExecutionAgentStore } from 'stores/execution-agents'
import { agentDisplayName } from 'src/features/computers/display'
import { useConfirm } from 'src/composables/confirm'
import { useAuthStore } from 'stores/auth'
import { useMcpServerStore } from 'stores/mcp-servers'
import PersonalSettingsGate from 'src/features/settings/PersonalSettingsGate.vue'

defineOptions({ name: 'McpSettingsPage' })
const { admin } = defineProps({ admin: { type: Boolean, default: false } })
const $q = useQuasar()
const route = useRoute()
const router = useRouter()
const oauthController = new AbortController()
const { confirm } = useConfirm()
const servers = useMcpServerStore()
const auth = useAuthStore()
const computers = useExecutionAgentStore()
const sessionsOpen = ref(false)
const sessionsServer = ref(null)
const dialogOpen = ref(false)
const selectedServer = ref(null)
const section = ref(route.query.oauthAttempt || route.query.oauthError ? 'management' : 'general')
const showManagement = computed(() => !admin || section.value === 'management')
const refreshingId = ref(null)
const removingId = ref(null)
const togglingId = ref(null)
const visibleServers = computed(() => servers.visibleServers.filter((server) => (
  admin ? server.scope === 'instance' : server.scope === 'personal' || server.oauth?.user_scoped === true
)))
const personalBlocked = computed(() => !admin && !auth.isAdmin && servers.settingsLoaded && !servers.personalAllowed)
const personalUnavailable = computed(() => (
  !servers.settingsLoaded || servers.settingsLoadFailed || !servers.personalAllowed && !auth.isAdmin
))

const canModifyScope = (server) => server.scope === 'instance' ? auth.isAdmin : !personalUnavailable.value
const canEdit = (server) => !server.deletedComputer && server.available !== false && canModifyScope(server)
const canRemove = (server) => canEdit(server) || (server.deletedComputer && canModifyScope(server))
const canConnect = (server) => server.available !== false
  && server.enabled
  && server.oauth?.user_scoped === true
  && (!server.bootstrapRequired || auth.isAdmin)
const canManageSessions = canEdit
function openSessions(server) { sessionsServer.value = server; sessionsOpen.value = true }
function computerName(server) { return agentDisplayName(computers.byId(server.executionAgentId)) || server.executionAgentName || 'Nicht verfügbarer Computer' }
function openCreate() { servers.selectScope(admin ? 'instance' : 'personal'); selectedServer.value = null; dialogOpen.value = true }
function openEdit(server) { servers.selectScope(server.scope); selectedServer.value = server; dialogOpen.value = true }
function onDialogChange(open) { if (!open) void load() }
function statusLabel(status) {
  return { unavailable: 'Nicht erreichbar', error: 'Fehler', stale: 'Tools veraltet' }[status] || status
}
async function load() { try { await servers.fetchVisibleServers() } catch { /* Central API toast. */ } }
async function loadPage() {
  void computers.fetchAll().catch(() => {})
  try {
    await servers.fetchSettings()
  } catch { return }
  await load()
}
async function saveSettings(enabled) {
  try {
    await servers.saveSettings(enabled)
    await load()
    $q.notify({ type: 'positive', message: 'MCP-Einstellungen gespeichert' })
  } catch { /* Central API toast. */ }
}
async function refreshServer(server) {
  servers.selectScope(server.scope)
  refreshingId.value = server.id
  try {
    await servers.refreshServer(server.id)
    await load()
    $q.notify({ type: 'positive', message: 'MCP-Tools aktualisiert' })
  } finally { refreshingId.value = null }
}
async function connectServer(server) {
  servers.selectScope(server.scope)
  try {
    const attempt = await servers.startOAuth({
      name: server.name,
      endpoint: server.endpoint,
      enabled: server.enabled,
      oauth: server.oauth,
      integrationId: server.id
    })
    window.location.assign(attempt.authorizationUrl)
  } catch {
    // The central API interceptor displays the error.
  }
}
async function disconnectServer(server) {
  if (!await confirm(`OAuth-Anmeldung für „${server.name}“ entfernen?`, 'Abmelden', { okLabel: 'Abmelden' })) return
  servers.selectScope(server.scope)
  try {
    await servers.disconnectOAuth(server)
    await load()
    $q.notify({ type: 'positive', message: 'OAuth-Anmeldung entfernt' })
  } catch {
    // The central API interceptor displays the error.
  }
}
async function toggleServer(server, enabled) {
  togglingId.value = server.id
  try {
    await servers.setServerEnabled(server.id, enabled, server.scope)
    await load()
    $q.notify({ type: 'positive', message: enabled ? 'MCP-Server aktiviert' : 'MCP-Server deaktiviert' })
  } catch {
    // The central API interceptor displays the error.
  } finally {
    togglingId.value = null
  }
}
async function removeServer(server) {
  if (!await confirm(`MCP-Server „${server.name}“ vollständig löschen?`, 'MCP-Server löschen', { okLabel: 'Löschen' })) return
  servers.selectScope(server.scope)
  removingId.value = server.id
  try {
    await servers.removeServer(server.id)
    await load()
    $q.notify({ type: 'positive', message: 'MCP-Server gelöscht' })
  } finally { removingId.value = null }
}
async function completeOAuth() {
  const id = route.query.oauthAttempt
  const error = route.query.oauthError
  if (!id && !error) return
  const query = { ...route.query }
  delete query.oauthAttempt
  delete query.oauthError
  await router.replace({ query })
  if (error) {
    notifyError({ code: 'mcp.oauthExpired' })
    return
  }
  try {
    await servers.completeOAuth(id, oauthController.signal)
    $q.notify({ type: 'positive', message: 'OAuth-Anmeldung abgeschlossen und MCP-Tools geladen' })
  } catch (failure) {
    if (failure?.code === 'mcp.oauthExpired') notifyError(failure)
    // Axios failures are displayed by the central interceptor.
  }
}
onBeforeUnmount(() => oauthController.abort())
onMounted(() => {
  if (!['settings-mcp-tools', 'admin-mcp-tools'].includes(route.name)) {
    void loadPage()
    void completeOAuth()
  }
})
</script>
