<template>
  <section class="column full-height no-wrap" aria-label="Session">
    <q-scroll-area class="col">
      <SessionSettingsFields
        :session="chat.activeContext"
        :agent-profile-id="chat.activeAgentProfileId"
        :disabled="chat.historyActionPending || chat.outboxSaving"
        :fields="['template', 'messageSize', 'agent', 'computer', 'memory', 'trust', 'webSearch', 'model', 'reasoning']"
        grouped
        show-warning
        class="q-pa-md"
        @update="chat.updateActive"
      />
      <div v-if="chat.running || chat.runId || chat.current?.activeRun" class="text-caption app-muted q-px-md q-pb-md">
        Änderungen gelten für die nächste Anfrage. Die laufende Anfrage behält ihre Einstellungen.
      </div>
      <template v-if="chat.current">
        <q-separator />
        <q-item class="q-px-md q-py-sm">
          <q-item-section avatar><q-icon :name="defaultChannel.icon" color="primary" /></q-item-section>
          <q-item-section>
            <q-item-label>Standardkanal</q-item-label>
            <q-item-label caption>{{ defaultChannel.label }}</q-item-label>
          </q-item-section>
        </q-item>
        <q-separator />
        <section class="q-pa-md" aria-labelledby="session-permissions-title">
          <div class="row items-center no-wrap q-mb-md">
            <div id="session-permissions-title" class="text-subtitle2">Freigaben</div>
            <q-space />
            <q-btn
              flat
              round
              dense
              icon="refresh"
              aria-label="Session-Freigaben aktualisieren"
              :loading="approvals.grantsLoading && loadedGrantContext !== grantContext"
              :disable="Boolean(revokingId)"
              @click="loadGrants"
            >
              <q-tooltip>Aktualisieren</q-tooltip>
            </q-btn>
            <q-btn
              v-if="explicitGrants.length && loadedGrantContext === grantContext && !approvals.grantsLoadFailed"
              flat
              round
              dense
              color="negative"
              icon="delete_sweep"
              aria-label="Alle Session-Freigaben widerrufen"
              :loading="revokingId === 'all'"
              :disable="Boolean(revokingId)"
              @click="revokeAll"
            >
              <q-tooltip>Alle Session-Freigaben widerrufen</q-tooltip>
            </q-btn>
          </div>
          <q-card v-if="approvals.grantsLoading && loadedGrantContext !== grantContext" flat class="app-surface">
            <q-card-section class="row items-center no-wrap q-gutter-md">
              <q-skeleton type="QAvatar" />
              <div class="col"><q-skeleton type="text" width="35%" /><q-skeleton type="text" width="75%" /></div>
            </q-card-section>
          </q-card>
          <q-card v-else-if="approvals.grantsLoadFailed" flat class="app-surface">
            <q-card-section class="column items-center text-center q-py-lg">
              <q-icon name="cloud_off" size="32px" class="app-muted" />
              <div class="q-mt-sm">Session-Freigaben konnten nicht geladen werden.</div>
              <q-btn class="q-mt-sm" flat no-caps color="primary" label="Erneut laden" @click="loadGrants" />
            </q-card-section>
          </q-card>
          <q-card v-else flat class="app-surface">
            <q-list separator>
              <q-item v-for="grant in sessionGrants" :key="grant.id">
                <q-item-section>
                  <q-item-label>
                    {{ grantDisplay(grant).label }}
                    <span v-if="grant.executionAgentName || grant.executionAgentId"> · {{ grant.executionAgentName || grant.executionAgentId }}</span>
                  </q-item-label>
                  <pre v-if="grant.resourceType !== 'shell_computer'" class="tool-call__details q-py-none">{{ grantDisplay(grant).value }}</pre>
                  <template v-if="grant.command">
                    <pre class="tool-call__details q-py-none">{{ grant.command }}</pre>
                  </template>
                  <q-item-label v-if="grant.elevated || grant.resourceType === 'administrator_path'" caption>Mit Administratorrechten</q-item-label>
                  <q-item-label caption>Geltungsbereich: Diese Session · Herkunft: {{ grant.source === 'default' ? 'Standard' : grant.source === 'profile' ? 'Profiloption (sessionübergreifende Regel)' : grant.source === 'session_input' ? 'Für diese Session gemerkt' : 'Erteilte Freigabe' }}</q-item-label>
                  <q-item-label v-if="grant.source === 'session_input'" caption>Nur temporär im Backend gespeichert; nach einem Backend-Neustart nicht mehr verfügbar. Erteilt keine Aktionsrechte.</q-item-label>
                  <q-item-label v-if="grant.derived" caption>{{ grant.source === 'profile' ? 'Über die Temp-Option im Agentenprofil ändern.' : 'Automatisch abgeleiteter Lesezugriff auf den eigenen temporären Arbeitsordner.' }}</q-item-label>
                  <q-item-label v-if="grant.applicable === false" caption class="text-warning">Inaktiv</q-item-label>
                </q-item-section>
                <q-item-section v-if="!grant.derived && !grant.readOnly" side>
                  <q-btn
                    flat
                    round
                    dense
                    icon="delete_outline"
                    color="negative"
                    :loading="revokingId === grant.id"
                    :disable="Boolean(revokingId)"
                    :aria-label="`${grantDisplay(grant).label} widerrufen`"
                    @click="revoke(grant)"
                  >
                    <q-tooltip>Freigabe widerrufen</q-tooltip>
                  </q-btn>
                </q-item-section>
              </q-item>
              <q-item v-if="!sessionGrants.length">
                <q-item-section class="app-muted">Keine Session-Freigaben vorhanden.</q-item-section>
              </q-item>
            </q-list>
          </q-card>
        </section>
      </template>
    </q-scroll-area>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import SessionSettingsFields from 'src/features/chat/components/SessionSettingsFields.vue'
import { useConfirm } from 'src/composables/confirm'
import { permissionGrantDisplay } from 'src/utils/approval-display'
import { useApprovalStore } from 'stores/approvals'
import { useChatStore } from 'src/features/chat/store'
import { useControlCenterStore } from 'stores/control-center'

defineOptions({ name: 'SessionApp' })

const approvals = useApprovalStore()
const chat = useChatStore()
const controlCenter = useControlCenterStore()
const $q = useQuasar()
const { confirm } = useConfirm()
const revokingId = ref(null)
const sessionGrants = computed(() => approvals.grantsForSession(chat.current?.id))
const explicitGrants = computed(() => sessionGrants.value.filter(grant => !grant.derived && !grant.readOnly))
const grantContext = computed(() => JSON.stringify([
  chat.current?.id, chat.current?.agentProfileId, chat.current?.modelId, chat.current?.sessionProfile,
  chat.current?.preferredExecutionAgentId, chat.current?.currentDirectory
]))
const loadedGrantContext = ref(null)
const visible = computed(() => controlCenter.open && controlCenter.activeContentId === 'session'
  && (!$q.screen.lt.md || !controlCenter.restoredOpen))
const defaultChannel = computed(() => {
  const channel = chat.current?.defaultChannel
  if (channel?.type === 'endpoint' && channel.provider === 'telegram') {
    return { icon: 'send', label: 'Telegram' }
  }
  return { icon: 'devices', label: 'MatoLoop-App' }
})
const grantDisplay = (grant) => permissionGrantDisplay(grant, chat.cachedSessions)

async function loadGrants() {
  if (!chat.current?.id) return
  const context = grantContext.value
  try {
    await approvals.fetchGrants({ suppressErrorToast: true }, chat.current.id)
    if (context === grantContext.value && !approvals.grantsLoadFailed) loadedGrantContext.value = context
  } catch { /* The retry state is displayed inline. */ }
}

async function revoke(grant) {
  const sessionId = chat.current?.id
  if (!sessionId) return
  const display = grantDisplay(grant)
  const accepted = await confirm(
    `Soll die Freigabe „${display.label}“ für ${[display.value, grant.executionAgentName || grant.executionAgentId, grant.command].filter(Boolean).join(' · ')} widerrufen werden?`,
    'Freigabe widerrufen',
    { okLabel: 'Widerrufen', okColor: 'negative' }
  )
  if (!accepted) return
  revokingId.value = grant.id
  try {
    await approvals.revoke(sessionId, grant.id)
    $q.notify({ type: 'positive', message: 'Freigabe widerrufen' })
  } finally {
    revokingId.value = null
  }
}

async function revokeAll() {
  const sessionId = chat.current?.id
  if (!sessionId || !explicitGrants.value.length) return
  const accepted = await confirm(
    `Sollen alle ${explicitGrants.value.length} Session-Freigaben widerrufen werden? Lauf-Freigaben und automatische Regeln gelten weiterhin. Gemerkte Session-Werte bleiben erhalten.`,
    'Alle Session-Freigaben widerrufen',
    { okLabel: 'Alle widerrufen', okColor: 'negative' }
  )
  if (!accepted) return
  revokingId.value = 'all'
  try {
    await approvals.revokeAll(sessionId)
    $q.notify({ type: 'positive', message: 'Session-Freigaben widerrufen' })
  } finally {
    revokingId.value = null
  }
}

watch(
  [grantContext, () => chat.runId, () => chat.current?.activeRun?.id, visible],
  ([context], previous = []) => {
    if (context !== previous[0]) loadedGrantContext.value = null
    if (visible.value) void loadGrants()
  },
  { immediate: true }
)
</script>
