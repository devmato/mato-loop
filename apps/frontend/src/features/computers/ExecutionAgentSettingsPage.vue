<template>
  <section>
    <header class="settings-section-header">
      <div>
        <h2 class="text-h6 text-weight-bold q-my-none">Computer</h2>
        <p class="text-body2 app-muted q-mb-none q-mt-xs">
          Lokale Datei- und Shell-Funktionen mit MatoLoop verbinden.
        </p>
      </div>
      <div class="settings-scope-actions row items-center col-auto">
        <q-btn
          v-if="updateCandidates.length || maintenanceBusy" outline no-caps color="primary" icon="system_update"
          :label="`Agents aktualisieren (${updateCandidates.length})`" :loading="maintenanceBusy"
          :disable="agents.loading || actionBusy || maintenanceBusy" @click="maintain(null, 'update')"
        />
        <q-btn color="primary" unelevated no-caps icon="add" label="Computer hinzufügen" :disable="maintenanceBusy" @click="openCreate" />
      </div>
    </header>

    <q-banner v-if="agents.loadFailed" rounded class="app-surface-tonal q-mb-md">
      Die Computer konnten nicht geladen werden.
      <template #action>
        <q-btn flat no-caps color="primary" label="Erneut laden" @click="load" />
      </template>
    </q-banner>

    <q-table
      flat bordered class="app-surface" row-key="id"
      :rows="agents.items" :columns="columns" :loading="agents.loading"
      :hide-no-data="agents.loadFailed"
      :visible-columns="$q.screen.lt.sm ? ['name', 'status', 'actions'] : undefined"
      :rows-per-page-options="[0]" :pagination="{ rowsPerPage: 0 }"
    >
      <template #no-data>
        <div v-if="agents.loading" role="status">Computer werden geladen …</div>
        <div v-else-if="agents.loaded" class="q-pa-md">
          <div>Noch kein Computer eingerichtet</div>
          <div class="text-caption app-muted">Füge einen Computer hinzu und verbinde ihn mit dem einmalig angezeigten Token.</div>
        </div>
      </template>
      <template #body-cell-name="props">
        <q-td :props="props">
          <q-btn flat no-caps color="primary" padding="none" :label="agentDisplayName(props.row)" :disable="actionBusy || maintenanceBusy" @click="openManage(props.row)" />
          <div v-if="props.row.id === agents.defaultExecutionAgentId" class="text-caption app-muted">Standardcomputer</div>
        </q-td>
      </template>
      <template #body-cell-version="props">
        <q-td :props="props">
          {{ props.row.agentVersion || 'Unbekannt' }}
          <q-icon
            v-if="props.row.agentVersion && props.row.agentVersion !== agents.backendVersion"
            name="info_outline" color="primary" size="xs" class="q-ml-xs"
            role="img" aria-hidden="false" tabindex="0"
            :aria-label="`Abweichende Agent-Version. ${agentVersionLabel(props.row.agentVersion, agents.backendVersion)}. MatoLoop-Version: ${agents.backendVersion || 'Unbekannt'}`"
          >
            <q-tooltip>{{ agentVersionLabel(props.row.agentVersion, agents.backendVersion) }} · MatoLoop-Version: {{ agents.backendVersion || 'Unbekannt' }}</q-tooltip>
          </q-icon>
        </q-td>
      </template>
      <template #body-cell-status="props">
        <q-td :props="props">
          <q-badge :color="statusColor(props.row)" :text-color="statusTextColor(props.row)" :label="statusLabel(props.row)" />
          <div v-if="props.row.maintenance" class="text-caption" role="status">{{ maintenanceLabel(props.row.maintenance) }}</div>
        </q-td>
      </template>
      <template #body-cell-actions="props">
        <q-td :props="props">
          <q-btn flat round icon="settings" color="primary" :aria-label="`${agentDisplayName(props.row)} verwalten`" :disable="actionBusy || maintenanceBusy" @click="openManage(props.row)" />
          <q-btn flat round icon="delete_outline" color="negative" :aria-label="`${agentDisplayName(props.row)} entfernen`" :disable="actionBusy || maintenanceBusy" @click="revoke(props.row)" />
        </q-td>
      </template>
    </q-table>

    <ExecutionAgentConfigDialog
      :model-value="configDialogOpen && (!managedAgentId || Boolean(managedAgent))"
      :agent="managedAgent"
      :busy="actionBusy || maintenanceBusy"
      @update:model-value="configDialogOpen = $event"
      @created="showPairingToken"
      @copy-command="copy($event, 'Installationsbefehl')"
    >
      <template v-if="managedAgent" #overview>
        <dl class="q-ma-none q-gutter-y-md">
          <div v-for="detail in computerDetails" :key="detail.label">
            <dt class="text-caption app-muted">{{ detail.label }}</dt>
            <dd class="q-ma-none settings-list-name">{{ detail.value }}</dd>
          </div>
        </dl>
        <div>
          <div class="text-subtitle2">{{ agentVersionLabel(managedAgent.agentVersion, agents.backendVersion) }}</div>
          <div v-if="!managedAgent.connected" class="text-caption app-muted">Zuletzt gemeldeter Agent-Stand</div>
        </div>
        <div>
          <div class="text-caption app-muted">Agent-Version · Commit-Datum (UTC) und Commit</div>
          <div>{{ managedAgent.agentVersion || 'Unbekannt' }}</div>
        </div>
        <div>
          <div class="text-caption app-muted">MatoLoop-Version · Referenz für Updates</div>
          <div>{{ agents.backendVersion || 'Unbekannt' }}</div>
        </div>
        <div class="text-body2 app-muted">
          Verglichen wird mit dieser MatoLoop-Instanz. Das Commit-Datum zeigt das Alter, nicht die neueste verfügbare Veröffentlichung. Ein Update übernimmt den MatoLoop-Stand und kann auch auf einen älteren Stand wechseln.
        </div>
        <div v-if="!agents.installationTarget" class="text-body2 app-muted">
          Für diesen MatoLoop-Stand ist kein reproduzierbares Web-Update verfügbar.
        </div>
        <div v-if="managedAgent.maintenance" class="text-body2" role="status">{{ maintenanceLabel(managedAgent.maintenance) }}</div>
      </template>
      <template v-if="managedAgent" #actions>
        <div v-if="!agents.installationTarget" class="text-body2 app-muted">Für diesen MatoLoop-Stand ist kein reproduzierbares Web-Update verfügbar.</div>
        <q-list>
          <q-item v-if="supportsUpdate(managedAgent)" clickable :disable="maintenanceDisabled || !agents.installationTarget" @click="maintain(managedAgent, 'update')">
            <q-item-section avatar><q-icon name="update" /></q-item-section>
            <q-item-section>Agent aktualisieren</q-item-section>
          </q-item>
          <q-item v-if="managedAgent.pairingState !== 'paired'" clickable :disable="actionBusy || maintenanceBusy" @click="openPairingToken(managedAgent)">
            <q-item-section avatar><q-icon name="key" /></q-item-section>
            <q-item-section>Neuen Kopplungstoken erstellen</q-item-section>
          </q-item>
          <q-item v-else clickable :disable="actionBusy || maintenanceBusy" @click="unpair(managedAgent)">
            <q-item-section avatar><q-icon name="link_off" color="negative" /></q-item-section>
            <q-item-section>Computer entkoppeln</q-item-section>
          </q-item>
          <q-item v-if="managedAgent.operations?.includes('maintenance.start')" clickable :disable="maintenanceDisabled" @click="maintain(managedAgent, 'uninstall')">
            <q-item-section avatar><q-icon name="delete_sweep" color="negative" /></q-item-section>
            <q-item-section>Agent deinstallieren</q-item-section>
          </q-item>
          <q-item clickable :disable="actionBusy || maintenanceBusy" @click="revoke(managedAgent)">
            <q-item-section avatar><q-icon name="delete_outline" color="negative" /></q-item-section>
            <q-item-section>Computer entfernen</q-item-section>
          </q-item>
        </q-list>
      </template>
    </ExecutionAgentConfigDialog>

    <q-dialog v-model="pairingDialogOpen" :transition-duration="0" :maximized="$q.screen.lt.sm" :persistent="saving">
      <q-card class="execution-agent-dialog">
        <q-form @submit.prevent="createPairingToken">
          <q-card-section>
            <div class="text-h6">Neuen Kopplungstoken erstellen</div>
            <q-banner v-if="pairingAgent?.pairingState === 'pending'" rounded class="app-surface-tonal q-mt-md">
              Der bisherige Kopplungstoken wird sofort ungültig.
            </q-banner>
          </q-card-section>
          <q-card-section class="q-pt-none">
            <AppSelect dense
              v-model="pairingDurationSeconds"
              outlined
              emit-value
              map-options
              :options="pairingDurationOptions"
              label="Gültigkeitsdauer des Kopplungstokens"
            />
          </q-card-section>
          <q-card-actions align="right" class="q-pa-md q-pt-none">
            <q-btn v-close-popup flat no-caps label="Abbrechen" :disable="saving" />
            <q-btn unelevated no-caps color="primary" label="Token erstellen" type="submit" :loading="saving" />
          </q-card-actions>
        </q-form>
      </q-card>
    </q-dialog>

    <q-dialog v-model="tokenDialogOpen" :transition-duration="0" persistent :maximized="$q.screen.lt.sm" @hide="pairingResult = null">
      <q-card class="execution-agent-dialog">
        <q-card-section>
          <div class="text-h6">Kopplungstoken</div>
          <q-banner rounded class="app-surface-tonal q-mt-md">
            Dieser Kopplungstoken wird nur einmal angezeigt. Bewahre ihn sicher auf und teile ihn nicht.
          </q-banner>
        </q-card-section>
        <q-card-section class="q-pt-none q-gutter-y-md">
          <q-input dense :model-value="pairingResult?.agent?.id" readonly outlined label="Computer-ID">
            <template #append>
              <q-btn flat round dense icon="content_copy" aria-label="Computer-ID kopieren" @click="copy(pairingResult?.agent?.id, 'Computer-ID')" />
            </template>
          </q-input>
          <q-input dense :model-value="pairingResult?.websocketUrl" readonly outlined label="WebSocket-Adresse">
            <template #append>
              <q-btn flat round dense icon="content_copy" aria-label="WebSocket-Adresse kopieren" @click="copy(pairingResult?.websocketUrl, 'WebSocket-Adresse')" />
            </template>
          </q-input>
          <q-input dense :model-value="formatExpiry(pairingResult?.pairingTokenExpiresAt)" readonly outlined label="Gültig bis" />
          <q-input dense :model-value="pairingResult?.pairingToken" readonly outlined type="textarea" autogrow label="Kopplungstoken">
            <template #append>
              <q-btn flat round dense icon="content_copy" aria-label="Kopplungstoken kopieren" @click="copy(pairingResult?.pairingToken, 'Kopplungstoken')" />
            </template>
          </q-input>
        </q-card-section>
        <q-card-actions align="right" class="q-pa-md q-pt-none">
          <q-btn unelevated no-caps color="primary" label="Schließen" @click="closeTokenDialog" />
        </q-card-actions>
      </q-card>
    </q-dialog>
  </section>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { copyToClipboard, useQuasar } from 'quasar'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import ExecutionAgentConfigDialog from 'src/features/computers/ExecutionAgentConfigDialog.vue'
import { agentDisplayName } from 'src/features/computers/display'
import { agentVersionLabel } from 'src/features/computers/version'
import { useConfirm } from 'src/composables/confirm'
import { formatAbsoluteDateTime } from 'src/utils/date-time'
import { apiErrorMessage, errorDescriptor } from 'src/utils/api-errors'
import { notifyError } from 'src/utils/notifications'
import { useExecutionAgentStore } from 'stores/execution-agents'

defineOptions({ name: 'ExecutionAgentSettingsPage' })

const pairingDurationChoices = [
  { label: '30 Minuten', value: 1800 },
  { label: '1 Stunde', value: 3600 },
  { label: '4 Stunden', value: 14_400 },
  { label: '12 Stunden', value: 43_200 },
  { label: '24 Stunden', value: 86_400 }
]
const expiryFormatter = new Intl.DateTimeFormat('de-DE', {
  day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short'
})
const $q = useQuasar()
const { confirm } = useConfirm()
const agents = useExecutionAgentStore()
const configDialogOpen = ref(false)
const pairingDialogOpen = ref(false)
const tokenDialogOpen = ref(false)
const pairingAgent = ref(null)
const pairingResult = ref(null)
const pairingDurationSeconds = ref(null)
const saving = ref(false)
const maintenanceBusy = ref(false)
const actionBusy = ref(false)
const managedAgentId = ref(null)
const managedAgent = computed(() => agents.byId(managedAgentId.value))
const maintenanceDisabled = computed(() => (
  actionBusy.value || maintenanceBusy.value || !canMaintain(managedAgent.value)
))
const updateCandidates = computed(() => agents.items.filter(needsUpdate))
const columns = [
  { name: 'name', label: 'Name', field: agentDisplayName, align: 'left', sortable: true },
  { name: 'platform', label: 'Betriebssystem', field: 'platform', format: value => value || 'Unbekannt', align: 'left' },
  { name: 'version', label: 'Agent-Version', field: 'agentVersion', align: 'left' },
  { name: 'status', label: 'Status', field: statusLabel, align: 'left' },
  { name: 'actions', label: 'Aktionen', field: 'id', align: 'right' }
]
const computerDetails = computed(() => managedAgent.value ? [
  { label: 'Computer-ID', value: managedAgent.value.id },
  { label: 'Computername', value: managedAgent.value.computerName || 'Noch nicht gemeldet' },
  { label: 'Betriebssystem', value: managedAgent.value.platform || 'Unbekannt' },
  { label: 'Status', value: statusLabel(managedAgent.value) },
  { label: 'Home-Verzeichnis', value: managedAgent.value.homeDirectory || 'Noch nicht gemeldet' },
  { label: 'Zuletzt gesehen', value: managedAgent.value.lastSeenAt ? formatAbsoluteDateTime(managedAgent.value.lastSeenAt) : 'Noch keine Verbindung' },
  ...(managedAgent.value.pairingState === 'pending' && managedAgent.value.pairingTokenExpiresAt
    ? [{ label: 'Kopplungstoken gültig bis', value: formatExpiry(managedAgent.value.pairingTokenExpiresAt) }] : [])
] : [])

function canMaintain(agent) {
  return agent?.connected && !(['running', 'unknown'].includes(agent.maintenance?.status)
    && Date.now() - Date.parse(agent.maintenance.startedAt) <= 65 * 60_000)
}

function supportsUpdate(agent) {
  return agent?.operations?.some(operation => ['maintenance.start', 'maintenance.update'].includes(operation))
}

function needsUpdate(agent) {
  return agents.installationTarget && canMaintain(agent) && supportsUpdate(agent)
    && agent.agentVersion !== agents.installationTarget.version
}
const pairingDurationOptions = computed(() => pairingDurationChoices.filter((option) => (
  option.value <= (agents.pairingPolicy?.maximumDurationSeconds || 0)
)))
function openCreate() {
  managedAgentId.value = null
  configDialogOpen.value = true
}

async function openPairingToken(agent) {
  try {
    const policy = await agents.fetchPairingPolicy()
    pairingDurationSeconds.value = policy.defaultDurationSeconds
  } catch {
    return
  }
  pairingAgent.value = agent
  pairingDialogOpen.value = true
}

function openManage(agent) {
  managedAgentId.value = agent.id
  configDialogOpen.value = true
}

function showPairingToken(result) {
  pairingResult.value = result
  tokenDialogOpen.value = true
}

async function createPairingToken() {
  if (!pairingAgent.value || saving.value) return
  saving.value = true
  try {
    pairingResult.value = await agents.createPairingToken(pairingAgent.value.id, pairingDurationSeconds.value)
    pairingDialogOpen.value = false
    tokenDialogOpen.value = true
  } finally {
    saving.value = false
  }
}

async function unpair(agent) {
  const accepted = await confirm(
    `„${agentDisplayName(agent)}“ entkoppeln? Der Computer wird sofort getrennt und muss für eine neue Verbindung erneut gekoppelt werden.`,
    'Computer entkoppeln',
    { okLabel: 'Entkoppeln', okColor: 'negative' }
  )
  if (!accepted) return
  actionBusy.value = true
  try {
    await agents.unpair(agent.id)
    $q.notify({ type: 'positive', message: 'Computer entkoppelt' })
  } finally {
    actionBusy.value = false
  }
}

async function revoke(agent) {
  const accepted = await confirm(
    `„${agentDisplayName(agent)}“ entfernen? Der Zugriff wird sofort widerrufen und der Computer aus der Liste entfernt.`,
    'Computer entfernen',
    { okLabel: 'Entfernen', okColor: 'negative' }
  )
  if (!accepted) return
  actionBusy.value = true
  try {
    await agents.revoke(agent.id)
    configDialogOpen.value = false
    managedAgentId.value = null
    $q.notify({ type: 'positive', message: 'Computer entfernt' })
  } finally {
    actionBusy.value = false
  }
}

function maintenanceLabel(job) {
  const action = job.action === 'update' ? 'Update' : 'Deinstallation'
  const result = {
    running: 'läuft', succeeded: 'abgeschlossen', failed: 'fehlgeschlagen', unknown: 'Ergebnis unbekannt'
  }[job.status] || 'Ergebnis unbekannt'
  return `${action}: ${result}${job.errorCode ? ` – ${apiErrorMessage({ code: job.errorCode })}` : ''}`
}

async function maintain(agent, action) {
  if (actionBusy.value || maintenanceBusy.value) return
  const selected = agent ? [agent] : [...updateCandidates.value]
  if (!selected.length) return
  maintenanceBusy.value = true
  try {
    if (action === 'update' && !await confirmUpdates(selected)) return
    if (action === 'uninstall' && !await confirm(
      `Agent auf „${agentDisplayName(agent)}“ deinstallieren? Programm und Dienst werden entfernt. Kopplungsdaten und Workspaces bleiben erhalten. Den Computereintrag kannst du anschließend separat entfernen.`,
      'Agent deinstallieren', { okLabel: 'Deinstallieren', okColor: 'negative' }
    )) return
    let started = 0
    for (const selectedAgent of selected) {
      const current = agent || agents.byId(selectedAgent.id)
      if (!agent && !needsUpdate(current)) continue
      if (await requestMaintenance(current, action)) started += 1
    }
    if (started) $q.notify({ type: 'positive', message: agent ? 'Wartung gestartet' : `Updates für ${started} ${started === 1 ? 'Agent' : 'Agents'} gestartet` })
  } finally {
    maintenanceBusy.value = false
  }
}

function confirmUpdates(selected) {
  const names = selected.map(agent => `„${agentDisplayName(agent)}“`).join(', ')
  const interruption = selected.some(agent => agent.operations?.includes('maintenance.update.interrupt'))
    ? 'Laufende MatoLoop-Aktionen werden abgebrochen. Offene MatoLoop-Terminals und Browser-Sitzungen werden geschlossen, auch aus anderen Chats. Nicht gespeicherte Eingaben können verloren gehen. '
    : ''
  const legacy = selected.some(agent => !agent.operations?.includes('maintenance.update.interrupt'))
    ? 'Ältere Agent-Versionen benötigen noch geschlossene Terminals und Browser sowie abgeschlossene Vorgänge. '
    : ''
  return confirm(
    `${names} auf den MatoLoop-Stand aktualisieren? Dieser kann auch älter sein. ${interruption}${legacy}Die Agents sind während des Updates vorübergehend nicht verfügbar.`,
    selected.length === 1 ? 'Agent aktualisieren' : 'Agents aktualisieren', { okLabel: 'Aktualisieren' }
  )
}

async function requestMaintenance(agent, action) {
  let password = null
  do {
    try {
      await agents.startMaintenance(agent.id, action, password, { suppressErrorToast: true })
      return true
    } catch (error) {
      const code = errorDescriptor(error)?.code
      if (code !== 'process.sudoAuthRequired' && code !== 'process.sudoAuthFailed') {
        $q.notify({ type: 'negative', message: `${agentDisplayName(agent)}: ${apiErrorMessage(error)}` })
        return false
      }
      if (password) notifyError(error)
      password = await new Promise((resolve) => {
        $q.dialog({
          transitionDuration: 0,
          color: 'primary',
          title: 'sudo-Passwort benötigt',
          message: `Gib das sudo-Passwort auf „${agentDisplayName(agent)}“ für diese Wartung ein. Es wird nur für diesen Auftrag verwendet.`,
          prompt: { model: '', type: 'password', label: 'sudo-Passwort', outlined: true, dense: true, maxlength: 4096, autocomplete: 'new-password', isValid: Boolean },
          cancel: { label: 'Abbrechen', color: undefined, class: 'app-muted', flat: true, noCaps: true },
          ok: { label: 'Wartung starten', color: 'primary', unelevated: true, noCaps: true },
          maximized: $q.screen.lt.sm
        }).onOk(resolve).onDismiss(() => resolve(null))
      })
    }
  } while (password !== null)
  return false
}

async function copy(value, label) {
  if (!value) return
  try {
    await copyToClipboard(value)
    $q.notify({ type: 'positive', message: `${label} kopiert` })
  } catch {
    $q.notify({ type: 'negative', message: `${label} konnte nicht kopiert werden` })
  }
}

function closeTokenDialog() {
  tokenDialogOpen.value = false
  pairingResult.value = null
}

function formatExpiry(value) {
  const date = value ? new Date(value) : null
  return date && !Number.isNaN(date.getTime()) ? expiryFormatter.format(date) : ''
}

function statusLabel(agent) {
  if (agent.pairingState === 'unpaired') return 'Nicht gekoppelt'
  if (agent.pairingState === 'pending') return 'Kopplung ausstehend'
  return agent.connected ? 'Verbunden' : 'Offline'
}

function statusColor(agent) {
  if (agent.pairingState === 'pending') return 'warning'
  if (agent.pairingState === 'unpaired') return 'blue-grey-8'
  if (agent.pairingState === 'paired' && agent.connected) return 'green-9'
  return 'grey-8'
}

function statusTextColor(agent) {
  return agent.pairingState === 'pending' ? 'black' : 'white'
}

async function load(config = null) {
  try {
    await agents.fetchAll(config)
  } catch {
    // Initial API errors use the central toast; polling keeps the inline state current.
  }
}

onMounted(() => {
  agents.startPolling()
  void agents.fetchPairingPolicy().catch(() => {})
})
onBeforeUnmount(() => agents.stopPolling())
</script>
