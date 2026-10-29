<template>
  <section>
    <header class="settings-section-header">
      <div><h2 class="text-h6 text-weight-bold q-my-none">Sessionvorlagen</h2><p class="text-body2 app-muted q-mb-none q-mt-xs">Agenten, Modelle, Memory und Nachrichtengröße für neue Sessions vorbelegen.</p></div>
      <q-btn v-if="showManagement" unelevated no-caps color="primary" icon="add" label="Vorlage erstellen" :disable="blocked" @click="edit()" />
    </header>
    <q-tabs v-if="admin" v-model="section" dense no-caps align="left" active-color="primary" class="q-mb-lg"><q-tab name="settings" label="Einstellungen" icon="settings" /><q-tab name="management" label="Verwaltung" icon="dashboard_customize" /></q-tabs>
    <PersonalSettingsGate v-if="admin && !showManagement" :model-value="store.personalSettingsEnabled" title="Persönliche Sessionvorlagen erlauben" description="Benutzer können eigene Vorlagen erstellen und verwenden." :disabled="blocked" @update:model-value="setEnabled" />
    <q-banner v-if="failed" class="app-surface-tonal q-mb-md">Sessionvorlagen konnten nicht geladen werden.<template #action><q-btn flat label="Erneut laden" @click="load" /></template></q-banner>
    <q-banner v-if="personalBlocked" class="app-surface-tonal q-mb-md">Persönliche Sessionvorlagen sind deaktiviert. Vorhandene Vorlagen bleiben gespeichert.</q-banner>
    <q-card v-if="showManagement" flat class="app-surface">
      <q-list separator>
        <q-item v-if="ready && !store.templates.length"><q-item-section>Noch keine Sessionvorlagen vorhanden.</q-item-section></q-item>
        <q-item v-for="item in store.templates" :key="item.id" class="q-py-md">
          <q-item-section><q-item-label>{{ item.name }}</q-item-label><q-item-label caption>{{ item.agentIds.map((id) => agentOptions.find((agent) => agent.value === id)?.label || 'Nicht verfügbar').join(', ') }}</q-item-label></q-item-section>
          <q-item-section side><div class="row no-wrap"><q-btn flat round dense icon="edit" :aria-label="`${item.name} bearbeiten`" :disable="blocked" @click="edit(item)" /><q-btn flat round dense icon="delete_outline" color="negative" :aria-label="`${item.name} löschen`" :disable="blocked || admin && item.id === store.defaultTemplateId" @click="remove(item)" /></div></q-item-section>
        </q-item>
      </q-list>
      <q-inner-loading :showing="store.loading" />
    </q-card>
    <q-dialog v-model="dialogOpen" :transition-duration="0" :maximized="$q.screen.lt.sm" persistent>
      <q-card class="app-surface column no-wrap full-width">
        <q-form class="column no-wrap full-height" @submit.prevent="save">
          <q-card-section class="row items-center"><div class="text-h6 col">Sessionvorlage</div><q-btn flat round dense icon="close" aria-label="Dialog schließen" :disable="store.saving" @click="close" /></q-card-section>
          <q-card-section class="col scroll q-gutter-md">
            <q-input v-model.trim="form.name" outlined dense label="Name" maxlength="80" :rules="[(value) => Boolean(value?.trim()) || 'Name eingeben.']" :disable="store.saving" />
            <div class="column q-gutter-y-md">
              <AppSelect :model-value="form.agentIds" outlined dense multiple use-chips emit-value map-options label="Verfügbare Agenten" :options="agentOptions" :disable="store.saving" :rules="[(value) => value?.length > 0 || 'Mindestens einen Agenten auswählen.']" @update:model-value="setAgents" />
              <AppSelect v-model="form.startAgentId" outlined dense emit-value map-options label="Startagent" :options="agentOptions.filter((item) => form.agentIds?.includes(item.value))" :disable="store.saving" />
              <AppSelect v-model="form.memoryMode" outlined dense emit-value map-options label="Memory" :options="memoryOptions" :disable="store.saving" />
              <AppSelect v-model="form.messageSize" outlined dense emit-value map-options label="Nachrichtengröße" :options="[{ label: 'Standard', value: 'standard' }, { label: 'Klein', value: 'small' }]" hint="Klein reduziert die Schriftgröße und die Abstände der Nachrichten sowie die Schriftgröße im Eingabefeld am Desktop." persistent-hint :disable="store.saving" />
              <div class="text-subtitle2">Modell für die gesamte Session</div>
              <ChatModelFields :model-value="form" :inherited="defaults" :disabled="store.saving" @update:model-value="Object.assign(form, $event)" />
              <q-expansion-item :duration="0" label="Modell-Overrides je Agent" icon="smart_toy">
                <q-card-section v-for="id in form.agentIds || []" :key="id">
                  <div class="text-subtitle2 q-mb-sm">{{ agentOptions.find((item) => item.value === id)?.label || 'Nicht verfügbarer Agent' }}</div>
                  <ChatModelFields :model-value="form.agentModels?.[id] || {}" :inherited="{ modelId: form.modelId || models.effective.profileModels?.[id]?.modelId || defaults.modelId, reasoningEffort: form.reasoningEffort || models.effective.profileModels?.[id]?.reasoningEffort || defaults.reasoningEffort }" :disabled="store.saving" @update:model-value="setOverride(id, $event)" />
                </q-card-section>
              </q-expansion-item>
            </div>
            <div class="text-caption app-muted">Wird beim Sessionstart übernommen. Bestehende Sessions bleiben unverändert.</div>
          </q-card-section>
          <q-card-actions align="right"><q-btn flat no-caps label="Abbrechen" :disable="store.saving" @click="close" /><q-btn unelevated no-caps color="primary" label="Speichern" type="submit" :loading="store.saving" /></q-card-actions>
        </q-form>
      </q-card>
    </q-dialog>
  </section>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { useAuthStore } from 'stores/auth'
import { useProviderStore } from 'stores/providers'
import { useAgentProfileStore } from 'stores/agent-profiles'
import { useConfirm } from 'src/composables/confirm'
import { randomUuid } from 'src/utils/random-id'
import { useSessionTemplateStore } from './session-templates-store'
import { useModelDefaultsStore } from './model-defaults-store'
import PersonalSettingsGate from './PersonalSettingsGate.vue'
import ChatModelFields from './ChatModelFields.vue'

const { admin } = defineProps({ admin: Boolean })
const auth = useAuthStore(), providers = useProviderStore(), agents = useAgentProfileStore(), store = useSessionTemplateStore(), models = useModelDefaultsStore(), $q = useQuasar()
const { confirm } = useConfirm()
const section = ref('settings'), ready = ref(false), failed = ref(false), dialogOpen = ref(false), form = ref({}), saved = ref('')
const scope = computed(() => admin ? 'instance' : 'personal')
const showManagement = computed(() => !admin || section.value === 'management')
const personalBlocked = computed(() => !admin && !auth.isAdmin && !store.personalSettingsEnabled)
const blocked = computed(() => !ready.value || failed.value || store.loading || store.saving || personalBlocked.value)
const agentOptions = computed(() => agents.agents.filter((agent) => !admin || agent.scope === 'instance').map((agent) => ({ label: agent.name, value: agent.id })))
const defaults = computed(() => ({ modelId: models.effective.defaultModelId, reasoningEffort: models.effective.reasoningEffort }))
const memoryOptions = [{ label: 'Vollständig', value: 'full' }, { label: 'Nur lesen', value: 'read_only' }, { label: 'Deaktiviert', value: 'disabled' }]
function setAgents(agentIds) {
  Object.assign(form.value, { agentIds, agentModels: Object.fromEntries(Object.entries(form.value.agentModels || {}).filter(([id]) => agentIds.includes(id))), ...(!agentIds.includes(form.value.startAgentId) ? { startAgentId: agentIds[0] || null } : {}) })
}
function setOverride(id, value) {
  const agentModels = form.value.agentModels ||= {}
  if (value.modelId || value.reasoningEffort) agentModels[id] = { modelId: value.modelId || null, reasoningEffort: value.reasoningEffort || null }
  else delete agentModels[id]
}
async function load() {
  ready.value = false; failed.value = false
  try {
    if (admin) { providers.selectScope('instance'); await providers.fetchAll() } else await providers.fetchEffective()
    await Promise.all([store.load(scope.value), agents.fetchAgents(admin ? 'instance' : null), models.load(scope.value)])
    ready.value = true
  } catch { failed.value = true }
}
function edit(item) {
  form.value = item ? JSON.parse(JSON.stringify(item)) : { id: randomUuid(), name: '', agentIds: [agents.defaultAgentProfileId], startAgentId: agents.defaultAgentProfileId, memoryMode: 'full', messageSize: 'standard', modelId: null, reasoningEffort: null, agentModels: {} }
  delete form.value.scope
  saved.value = JSON.stringify(form.value); dialogOpen.value = true
}
const discard = () => !store.saving && (!dialogOpen.value || JSON.stringify(form.value) === saved.value || confirm('Ungespeicherte Änderungen verwerfen?'))
async function close() { if (await discard()) dialogOpen.value = false }
async function save() {
  try { await store.save(scope.value, form.value); dialogOpen.value = false; $q.notify({ type: 'positive', message: 'Sessionvorlage gespeichert' }) } catch { /* Central API toast. */ }
}
async function remove(item) {
  if (!await confirm(`Vorlage „${item.name}“ löschen? Bestehende Sessions bleiben erhalten.`, 'Sessionvorlage löschen?', { okLabel: 'Löschen' })) return
  try { await store.remove(scope.value, item.id); $q.notify({ type: 'positive', message: 'Sessionvorlage gelöscht' }) } catch { /* Central API toast. */ }
}
async function setEnabled(enabled) {
  try { await store.setPersonalEnabled(enabled); $q.notify({ type: 'positive', message: 'Einstellung gespeichert' }) } catch { /* Central API toast. */ }
}
onBeforeRouteLeave(discard)
onBeforeRouteUpdate(discard)
watch(scope, load, { immediate: true })
</script>
