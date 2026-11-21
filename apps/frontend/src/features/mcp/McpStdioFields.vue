<template>
  <AppSelect dense v-model="form.executionAgentId" outlined emit-value map-options label="Computer" :options="computerOptions" :loading="computers.loading" :disable="disabled" lazy-rules :rules="[requiredRule]">
    <template #no-option><q-item><q-item-section>Kein Computer eingerichtet</q-item-section></q-item></template>
  </AppSelect>
  <q-banner v-if="computers.loadFailed" rounded class="app-surface-tonal">
    Die Computer konnten nicht geladen werden.
    <template #action><q-btn flat no-caps label="Erneut laden" :disable="disabled" @click="loadComputers" /></template>
  </q-banner>
  <q-input dense v-model.trim="form.executable" outlined label="Ausführbare Datei" hint="Absoluter Pfad auf dem gewählten Computer; kein Shell-Befehl." persistent-hint :disable="disabled" lazy-rules :rules="[requiredRule, absolutePathRule]" />
  <q-input dense v-model="form.argsText" outlined type="textarea" autogrow label="Argumente (JSON-Array)" hint='Jedes Argument als eigene Zeichenkette, z. B. ["/opt/mcp/server.js"]. Ohne Argumente: []' persistent-hint :disable="disabled" lazy-rules :rules="[argsRule]" />
  <q-input dense v-model.trim="form.workingDirectory" outlined label="Arbeitsverzeichnis" hint="Absoluter Pfad auf dem gewählten Computer." :disable="disabled" lazy-rules :rules="[requiredRule, absolutePathRule]" />
  <q-input dense v-model.number="form.idleTimeoutSeconds" outlined type="number" min="1" max="86400" step="1" label="Leerlaufzeitlimit (Sekunden)" hint="Standard: 1800 Sekunden (30 Minuten)." :disable="disabled" lazy-rules :rules="[timeoutRule]" />
  <q-banner rounded class="app-surface-tonal" role="alert">
    MatoLoop startet das Programm auf diesem Computer. Verwende nur vertrauenswürdige Programme. Jede Chat-Sitzung erhält einen eigenen Prozess. Nach dem Leerlaufzeitlimit wird er beendet; nicht gespeicherter Zustand geht verloren.
  </q-banner>
  <q-input dense v-model="form.environmentText" outlined type="textarea" autogrow label="Umgebungsvariablen (JSON-Objekt, ohne Geheimnisse)" hint='Optional, z. B. {"LANG": "de_DE.UTF-8"}. Zugangsdaten nur unten als Geheimnisse hinterlegen.' persistent-hint :disable="disabled" lazy-rules :rules="[environmentRule]" />
  <div class="text-subtitle2">Geheime Umgebungsvariablen</div>
  <div class="text-caption app-muted">Werte werden verschlüsselt gespeichert und nicht erneut angezeigt. Gespeicherte Werte bleiben unverändert, solange du sie nicht ersetzt oder entfernst.</div>
  <q-checkbox v-for="key in secretKeys" :key="key" dense v-model="form.removeEnvironmentSecrets" :val="key" :label="`${key} entfernen`" color="negative" :disable="disabled" />
  <div v-for="(secret, index) in form.secrets" :key="index" class="row items-start q-col-gutter-sm">
    <q-input dense v-model.trim="secret.key" outlined label="Variablenname" class="col-12 col-sm-5" :disable="disabled" lazy-rules :rules="[secretNameRule]" />
    <q-input dense v-model="secret.value" outlined type="password" autocomplete="new-password" label="Geheimer Wert" class="col" :disable="disabled" lazy-rules :rules="[requiredRule]" />
    <div class="col-auto q-pt-sm"><q-btn flat round dense icon="delete" :aria-label="`Geheimnis ${index + 1} entfernen`" :disable="disabled" @click="form.secrets.splice(index, 1)" /></div>
  </div>
  <q-btn outline no-caps color="primary" icon="add" label="Geheimnis hinzufügen oder ersetzen" :disable="disabled" @click="form.secrets.push({ key: '', value: '' })" />
</template>

<script setup>
import { computed, onMounted } from 'vue'
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { useExecutionAgentStore } from 'stores/execution-agents'
import { agentDisplayName } from 'src/features/computers/display'
import { absolutePathRule, argsRule, environmentRule } from './stdio-config'

const form = defineModel({ type: Object, required: true })
defineProps({ disabled: Boolean, secretKeys: { type: Array, default: () => [] } })
const computers = useExecutionAgentStore()
const computerOptions = computed(() => {
  const options = computers.items.map((computer) => ({ label: `${agentDisplayName(computer)}${computer.connected ? '' : ' (offline)'}`, value: computer.id }))
  if (form.value.executionAgentId && !computers.byId(form.value.executionAgentId)) {
    options.unshift({ label: 'Nicht verfügbarer Computer', value: form.value.executionAgentId })
  }
  return options
})
const requiredRule = (value) => Boolean(String(value || '').trim()) || 'Dieses Feld ist erforderlich.'
const timeoutRule = (value) => Number.isInteger(Number(value)) && Number(value) >= 1 && Number(value) <= 86400 || 'Gib 1 bis 86400 ganze Sekunden ein.'
function secretNameRule(value) {
  return (/^[A-Za-z_][A-Za-z0-9_]*$/.test(value)
    && form.value.secrets.filter((secret) => secret.key.trim() === value).length === 1
    && !form.value.removeEnvironmentSecrets.includes(value)
    && !(environmentRule(form.value.environmentText) === true && Object.hasOwn(JSON.parse(form.value.environmentText), value)))
    || 'Verwende einen eindeutigen Variablennamen ohne Leerzeichen; entfernte oder öffentliche Variablen nicht gleichzeitig ersetzen.'
}
async function loadComputers() {
  try { await computers.fetchAll() } catch { /* Central API toast. */ }
}
onMounted(loadComputers)
</script>
