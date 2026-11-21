<template>
  <q-dialog :model-value="modelValue" :transition-duration="0" :maximized="$q.screen.lt.sm" :persistent="Boolean(closingId)" @hide="close">
    <q-card class="integration-dialog app-surface column">
      <q-card-section class="row items-start no-wrap q-gutter-md">
        <q-avatar color="primary" text-color="white" icon="terminal" />
        <div class="col"><div class="text-h6">Aktive Sitzungen</div><div class="text-body2 app-muted">{{ server?.name }}</div></div>
        <q-btn flat round dense icon="close" aria-label="Dialog schließen" :disable="Boolean(closingId)" @click="close" />
      </q-card-section>
      <q-separator />
      <q-card-section class="col scroll">
        <q-banner rounded class="app-surface-tonal q-mb-md">Jede Chat-Sitzung verwendet einen eigenen Prozess. Beim Beenden oder nach Ablauf des Leerlaufzeitlimits geht nicht gespeicherter Zustand verloren. Laufende Aufrufe müssen erst abgeschlossen sein. Aktualisiere die Liste danach.</q-banner>
        <q-banner v-if="loadFailed" rounded class="app-surface-tonal">Die Sitzungen konnten nicht geladen werden.</q-banner>
        <div v-else-if="loading" role="status"><q-spinner class="q-mr-sm" />Sitzungen werden geladen …</div>
        <div v-else-if="!sessions.length" class="app-muted">Keine aktiven Sitzungen</div>
        <q-list v-else separator>
          <q-item v-for="session in sessions" :key="session.id" class="q-px-none">
            <q-item-section>
              <q-item-label class="text-wrap">{{ session.sessionId === server?.id ? 'Tool-Erkennung' : session.sessionId }}</q-item-label>
              <q-item-label caption>{{ session.busy ? 'In Verwendung' : 'Im Leerlauf' }} · Zeitlimit: {{ formatTimeout(session.idleTimeoutSeconds) }}</q-item-label>
              <q-item-label v-if="session.expiresAt" caption>Leerlaufende: {{ formatExpiry(session.expiresAt) }}</q-item-label>
            </q-item-section>
            <q-item-section side><q-btn flat no-caps color="negative" label="Beenden" :disable="session.busy || Boolean(closingId)" :loading="closingId === session.id" @click="endSession(session)" /></q-item-section>
          </q-item>
        </q-list>
      </q-card-section>
      <q-separator />
      <q-card-actions class="q-pa-md"><q-btn flat no-caps label="Schließen" :disable="Boolean(closingId)" @click="close" /><q-space /><q-btn outline no-caps color="primary" icon="refresh" label="Aktualisieren" :loading="loading" :disable="Boolean(closingId)" @click="load" /></q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup>
import { ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useMcpServerStore } from 'stores/mcp-servers'
import { useConfirm } from 'src/composables/confirm'

const props = defineProps({ modelValue: Boolean, server: { type: Object, default: null } })
const emit = defineEmits(['update:modelValue'])
const $q = useQuasar()
const servers = useMcpServerStore()
const { confirm } = useConfirm()
const sessions = ref([])
const loading = ref(false)
const loadFailed = ref(false)
const closingId = ref(null)
let loadVersion = 0
function close() { if (!closingId.value) emit('update:modelValue', false) }
function formatExpiry(value) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Unbekannt' : date.toLocaleString('de-DE')
}
function formatTimeout(seconds) {
  if (seconds === 60) return '1 Minute'
  if (seconds % 60 === 0) return `${seconds / 60} Minuten`
  return seconds === 1 ? '1 Sekunde' : `${seconds} Sekunden`
}
async function load() {
  const version = ++loadVersion
  loading.value = true
  loadFailed.value = false
  try {
    const result = await servers.fetchSessions(props.server)
    if (version === loadVersion) sessions.value = result
  } catch {
    if (version === loadVersion) loadFailed.value = true
  } finally {
    if (version === loadVersion) loading.value = false
  }
}
async function endSession(session) {
  const server = props.server
  if (session.busy || !await confirm('Diese MCP-Sitzung beenden? Nicht gespeicherter Zustand geht verloren.', 'MCP-Sitzung beenden', { okLabel: 'Beenden' })) return
  if (!props.modelValue || props.server !== server || closingId.value) return
  closingId.value = session.id
  try {
    if (await servers.closeSession(server, session.sessionId, session.id)) {
      $q.notify({ type: 'positive', message: 'MCP-Sitzung beendet' })
    }
  } catch { /* Central API toast. */ } finally {
    if (props.modelValue && props.server === server) await load()
    closingId.value = null
  }
}
watch(() => [props.modelValue, props.server], () => {
  sessions.value = []
  loadVersion += 1
  if (props.modelValue && props.server) void load()
}, { immediate: true })
</script>
