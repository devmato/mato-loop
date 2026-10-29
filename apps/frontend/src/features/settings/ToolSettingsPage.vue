<template>
  <section>
    <header class="settings-section-header">
      <div>
        <h2 class="text-h6 text-weight-bold q-my-none">Tools</h2>
        <p class="text-body2 app-muted q-mb-none q-mt-xs">{{ admin ? 'Globale Tool-Vorgaben und eigene Profileinstellungen verwalten.' : 'Tools und Aktionsrechte der eigenen Agentenprofile anpassen.' }}</p>
      </div>
      <q-btn v-if="canEdit" color="primary" unelevated no-caps icon="save" label="Speichern" :loading="saving" :disable="loading || !dirty" @click="save" />
    </header>
    <q-tabs :model-value="section" dense no-caps align="left" active-color="primary" indicator-color="primary" class="q-mb-lg" @update:model-value="selectSection">
      <q-tab v-if="admin" name="general" icon="settings" label="Einstellungen" />
      <q-tab name="availability" icon="build" label="Tool-Einstellungen" />
      <q-tab name="management" icon="verified_user" label="Berechtigungen" />
    </q-tabs>
    <AppSelect v-if="section !== 'general'" :model-value="selectedId" dense outlined emit-value map-options label="Agentenprofil" class="q-mb-md" :options="profileOptions" :disable="loading || saving" @update:model-value="selectProfile" />
    <q-banner v-if="!canEdit && !loading" rounded class="app-surface-tonal">
      <template #avatar><q-icon name="admin_panel_settings" color="primary" /></template>
      {{ !allowed ? 'Persönliche Tool-Anpassungen wurden für diese Instanz deaktiviert.' : 'Erstelle zuerst ein persönliches Agentenprofil.' }}
    </q-banner>
    <q-banner v-else-if="loadFailed" rounded class="bg-negative text-white q-mb-md">
      <template #avatar><q-icon name="error_outline" /></template>
      Die Tool-Einstellungen konnten nicht geladen werden.
      <template #action><q-btn flat no-caps color="white" label="Erneut laden" @click="load" /></template>
    </q-banner>
    <q-form v-else-if="canEdit" ref="formRef" @submit.prevent="save">
      <q-card v-if="admin && section === 'general'" flat class="app-surface q-mb-md">
        <q-card-section class="row items-center q-gutter-md">
          <q-avatar color="primary" text-color="white" icon="verified_user" />
          <div class="col">
            <div class="text-subtitle1 text-weight-bold">Freigabeverhalten</div>
            <div class="text-caption app-muted">Lege fest, welche Aktionen der Agent standardmäßig automatisch ausführt und wann er um Erlaubnis fragt.</div>
          </div>
        </q-card-section>
        <q-separator />
        <q-card-section>
          <div class="row q-col-gutter-lg">
            <div v-for="audience in audiences" :key="audience.value" :class="auth.modelTrustEnabled ? 'col-12 col-md-6' : 'col-12'">
              <div :id="`preset-${audience.value}`" class="text-subtitle2 text-weight-bold q-mb-sm">{{ audience.label }}</div>
              <div role="radiogroup" :aria-labelledby="`preset-${audience.value}`">
                <div v-for="option in presetOptions" :key="option.value" class="row no-wrap items-center q-py-xs">
                  <q-radio v-model="selectedPresets[audience.value]" class="col" :name="`preset-${audience.value}`" :val="option.value" :label="option.label" :aria-label="`${audience.label}: ${option.label}`" :disable="loading || saving || loadFailed" />
                  <span v-if="option.value === audience.default" class="text-caption app-muted q-mx-xs">Vorgabe</span>
                  <q-btn flat round dense icon="info_outline" :aria-label="`${audience.label}: ${option.label} erklären`" @click="presetInfo = option; presetDialogOpen = true" />
                </div>
              </div>
            </div>
          </div>
        </q-card-section>
      </q-card>
      <template v-if="admin && section === 'general' && form.configuration.webSearch && form.configuration.webfetch">
        <q-card v-for="provider in providerFields" :key="provider.key" flat class="app-surface q-mb-md">
          <q-card-section>
            <div class="text-subtitle1 text-weight-bold q-mb-sm">{{ provider.label }}</div>
            <div class="row q-col-gutter-md">
              <AppSelect v-model="form.configuration[provider.key].primaryProvider" class="col-12 col-md-6" dense outlined emit-value map-options label="Anbieter" :options="providerOptions(provider.key)" :disable="loading || saving" />
            </div>
          </q-card-section>
          <template v-if="provider.key === 'webfetch' && form.configuration.webAccess">
            <q-separator />
            <q-card-section>
              <q-toggle v-model="form.configuration.webAccess.publicAddressesOnly" color="primary" label="Nur öffentliche Zieladressen zulassen" :disable="loading || saving" />
              <div class="text-caption app-muted">Begrenzt direkte Webseitenabrufe, Dateidownloads und Untertitelabrufe auf öffentliche Netzwerkadressen, auch beim URL-Import in Integrationen. Jina Reader, Shell, Computer-Browser und Verbindungen zu konfigurierten Diensten haben eigene Zugriffsregeln.</div>
              <div class="row q-col-gutter-md q-mt-sm">
                <q-input v-model.trim="form.configuration.webAccess.userAgent" class="col-12 col-md-8" outlined maxlength="512" label="User-Agent" :rules="[userAgentRule]" :disable="loading || saving" />
                <q-input v-model.number="form.configuration.webAccess.maxResponseMiB" class="col-12 col-md-4" outlined type="number" min="1" max="256" suffix="MiB" label="Maximale Antwortgröße" :rules="[responseLimitRule]" :disable="loading || saving" />
              </div>
            </q-card-section>
          </template>
        </q-card>
      </template>
      <PersonalSettingsGate v-if="admin && section === 'general'" v-model="form.personalSettingsEnabled" title="Tool-Rechte für persönliche Agentenprofile anpassen erlauben" description="Benutzer können Tools, Shell-Präfixe und Aktionsrechte ihrer eigenen Profile anpassen." :disabled="saving || loading" />
      <ToolConfigurationRules v-if="section === 'availability' && selectedId" :key="selectedId" v-model="form.configuration" :groups="permissionGroups.private" :inherited-shell-prefixes="inheritedShellPrefixes" :inherited-availability="inheritedAvailability" :global-availability="globalAvailability" :disabled="loading || saving || loadFailed" />
      <template v-if="section === 'availability' && !selectedId">
        <q-card v-for="group in technicalGroups" :key="group.id" flat class="app-surface q-mb-md">
          <q-card-section class="row items-center q-gutter-md">
            <q-icon :name="group.icon || 'build'" color="primary" size="sm" />
            <div class="text-subtitle1 text-weight-bold">{{ group.label }}</div>
          </q-card-section>
          <template v-for="tool in group.tools" :key="tool.name">
            <q-separator />
            <q-card-section>
              <div class="row items-center q-col-gutter-md">
                <div class="col row items-center no-wrap q-gutter-sm">
                  <q-icon :name="tool.icon || 'build'" color="primary" size="sm" />
                  <div><div class="text-subtitle2 text-weight-bold">{{ tool.label }}</div><div class="text-caption app-muted">{{ tool.name }}</div></div>
                </div>
                <div class="col-auto"><q-toggle :model-value="form.configuration.toolAvailability[tool.name] !== false" color="primary" label="Verfügbar" :aria-label="`${tool.label}: verfügbar`" :disable="loading || saving || loadFailed" @update:model-value="form.configuration.toolAvailability[tool.name] = $event" /></div>
              </div>
              <template v-if="form.configuration.toolAvailability[tool.name] !== false">
                <q-input v-if="mediaByName[tool.name]" v-model.number="form.configuration[mediaByName[tool.name]].timeoutSeconds" class="q-mt-md" outlined type="number" min="10" max="3600" label="Zeitlimit" suffix="Sekunden" :rules="[timeoutRule]" :disable="loading || saving" />
                <div v-if="tool.name === 'shell_run'" class="row justify-end q-mt-sm"><q-btn flat no-caps icon="edit" label="Shell-Präfixe konfigurieren" :disable="loading || saving" @click="editShellPrefixes" /></div>
              </template>
            </q-card-section>
          </template>
        </q-card>
      </template>
      <template v-if="section === 'management'">
        <q-banner v-if="!selectedId" rounded class="app-surface-tonal q-mb-md">Eigene Regeln ändern das Freigabeverhalten. Ein technisch nicht verfügbares Tool wird dadurch nicht aktiviert.</q-banner>
        <ProfileToolOverrides :key="selectedId || 'global'" v-model="form.permissions" :groups-by-trust="permissionGroups" :model-trust-enabled="auth.modelTrustEnabled" :disabled="loading || saving || loadFailed">
          <template #actions>
            <q-btn v-if="selectedId && canEdit" flat round dense icon="delete_outline" color="negative" aria-label="Alle eigenen Regeln entfernen" :disable="loading || saving || loadFailed || !hasOverrides" @click="resetPermissions" />
          </template>
        </ProfileToolOverrides>
      </template>
      <q-inner-loading :showing="loading" />
    </q-form>
    <q-dialog v-model="presetDialogOpen" :maximized="$q.screen.lt.sm" aria-labelledby="preset-info-title">
      <q-card v-if="presetInfo" class="app-surface column no-wrap full-width">
        <q-card-section class="row items-center no-wrap">
          <div id="preset-info-title" class="text-h6 col">{{ presetInfo.label }}</div>
          <q-btn v-close-popup flat round dense icon="close" aria-label="Dialog schließen" />
        </q-card-section>
        <q-separator />
        <q-card-section class="col scroll">
          <div class="text-subtitle2 text-weight-bold q-mb-sm">Basisberechtigungen</div>
          <div v-for="rule in presetInfo.baseline" :key="rule.category" class="row justify-between q-col-gutter-md q-py-xs">
            <div class="col">{{ rule.label }}</div>
            <div class="col-auto">{{ rule.policy === 'auto' ? 'Automatisch' : 'Nachfragen' }}</div>
          </div>
          <div class="text-subtitle2 text-weight-bold q-mt-lg q-mb-sm">Zusätzlich automatisch</div>
          <ul v-if="presetInfo.additionalPermissions.length" class="q-my-none q-pl-md">
            <li v-for="label in presetInfo.additionalPermissions" :key="label" class="q-mb-xs">{{ label }}</li>
          </ul>
          <div v-else class="app-muted">Keine</div>
          <div class="text-subtitle2 text-weight-bold q-mt-lg q-mb-sm">Zusätzlich nachfragen</div>
          <ul v-if="presetInfo.additionalConfirmations.length" class="q-my-none q-pl-md">
            <li v-for="label in presetInfo.additionalConfirmations" :key="label" class="q-mb-xs">{{ label }}</li>
          </ul>
          <div v-else class="app-muted">Keine</div>
          <div class="text-caption app-muted q-mt-lg">Die Listen zeigen nur Ausnahmen von den Basisberechtigungen. Eigene Regeln und Profilvorgaben haben Vorrang. Zugriffsgrenzen gelten weiterhin.</div>
        </q-card-section>
        <q-card-actions align="right"><q-btn v-close-popup flat no-caps label="Schließen" /></q-card-actions>
      </q-card>
    </q-dialog>
    <q-dialog v-model="shellDialogOpen" :maximized="$q.screen.lt.sm" aria-labelledby="shell-prefixes-title">
      <q-card class="app-surface column no-wrap full-width">
        <q-card-section class="row items-center"><div id="shell-prefixes-title" class="text-h6 col">Shell-Präfixe</div><q-btn v-close-popup flat round dense icon="close" aria-label="Dialog schließen" /></q-card-section>
        <q-card-section class="col scroll"><ShellPrefixFields v-model="shellDraft" /></q-card-section>
        <q-card-actions align="right"><q-btn v-close-popup flat no-caps label="Abbrechen" /><q-btn unelevated no-caps color="primary" label="Übernehmen" @click="applyShellPrefixes" /></q-card-actions>
      </q-card>
    </q-dialog>
  </section>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { onBeforeRouteLeave, useRoute } from 'vue-router'
import { useConfirm } from 'src/composables/confirm'
import { useAuthStore } from 'stores/auth'
import { useToolSettingsStore } from 'stores/tool-settings'
import { useAgentProfileStore } from 'stores/agent-profiles'
import ProfileToolOverrides from './ProfileToolOverrides.vue'
import ToolConfigurationRules from './ToolConfigurationRules.vue'
import ShellPrefixFields from './ShellPrefixFields.vue'
import PersonalSettingsGate from './PersonalSettingsGate.vue'

defineOptions({ name: 'ToolSettingsPage' })
const { admin } = defineProps({ admin: { type: Boolean, default: false } })
const $q = useQuasar()
const { confirm } = useConfirm()
const auth = useAuthStore()
const agents = useAgentProfileStore()
const route = useRoute()
const tools = useToolSettingsStore()
const selectedId = ref(route.query.profileId || null)
const initialSection = () => route.query.profileId ? 'management' : ['management', 'availability'].includes(route.query.tab) ? route.query.tab : admin ? 'general' : 'availability'
const section = ref(initialSection())
const permissionGroups = ref({ private: [], public: [] })
const inheritedShellPrefixes = ref({ read: [], write: [] })
const technicalGroups = ref([])
const inheritedAvailability = ref({})
const globalAvailability = ref({})
const permissionsLoading = ref(false)
let loadToken = 0
const allowed = computed(() => admin || auth.isAdmin || (agents.personalAgentsEnabled && agents.personalToolPermissionsEnabled))
const canEdit = computed(() => allowed.value && (admin || Boolean(selectedId.value)))
const profileOptions = computed(() => [
  ...(admin ? [{ label: 'Globaler Standard', value: null }] : []),
  ...agents.manageableAgents.filter(agent => admin ? agent.scope === 'instance' : agent.scope === 'personal').map(agent => ({ label: agent.name, value: agent.id }))
])
const formRef = ref(null)
const saving = ref(false)
const loadFailed = ref(false)
const saved = ref('')
const selectedPresets = ref({ private: 'ask_changes', public: 'balanced' })
const audiences = computed(() => auth.modelTrustEnabled
  ? [{ value: 'private', label: 'Privat', default: 'ask_changes' }, { value: 'public', label: 'Öffentlich', default: 'balanced' }]
  : [{ value: 'private', label: 'Alle Modelle', default: 'ask_changes' }])
const presetOptions = computed(() => tools.presetOptions)
const presetInfo = ref(null)
const presetDialogOpen = ref(false)
const mediaByName = { generate_image: 'generateImage', edit_image: 'editImage', generate_speech: 'generateSpeech', generate_audio: 'generateAudio', transcribe_audio: 'transcribeAudio', analyze_video: 'analyzeVideo', analyze_audio: 'analyzeAudio' }
const mediaFields = Object.values(mediaByName)
const providerFields = [{ key: 'webSearch', label: 'Websuche' }, { key: 'webfetch', label: 'Webabrufe und Downloads' }]
const webProviderOptions = ref({ webSearch: [], webfetch: [] })
function providerOptions(field) {
  const options = webProviderOptions.value[field].map(item => ({ label: item.name, value: item.value }))
  for (const value of Object.values(form.configuration[field] || {})) {
    if (value && !options.some(item => item.value === value)) options.push({ label: 'Nicht verfügbarer Anbieter', value, disable: true })
  }
  return options
}
const shellDialogOpen = ref(false)
const shellDraft = ref({ shellCustom: true, shellRead: '', shellWrite: '' })
const timeoutRule = value => Number.isInteger(value) && value >= 10 && value <= 3600 || '10 bis 3600 Sekunden eingeben.'
const responseLimitRule = value => Number.isInteger(value) && value >= 1 && value <= 256 || '1 bis 256 MiB eingeben.'
const userAgentRule = value => typeof value === 'string' && value.length >= 1 && value.length <= 512 && [...value].every(character => character.charCodeAt(0) >= 32 && character.charCodeAt(0) <= 126) || '1 bis 512 sichtbare ASCII-Zeichen eingeben.'
const prefixes = value => value.split('\n').map(line => line.trim().split(/\s+/).filter(Boolean)).filter(tokens => tokens.length)
function editShellPrefixes() {
  shellDraft.value = { shellCustom: true, shellRead: form.configuration.shellPrefixes.read.map(item => item.join(' ')).join('\n'), shellWrite: form.configuration.shellPrefixes.write.map(item => item.join(' ')).join('\n') }
  shellDialogOpen.value = true
}
function applyShellPrefixes() {
  form.configuration.shellPrefixes = { read: prefixes(shellDraft.value.shellRead), write: prefixes(shellDraft.value.shellWrite) }
  shellDialogOpen.value = false
}
const emptyPermissions = () => ({ privatePermissions: { policies: {}, integrationPolicies: {}, resourcePolicies: {} }, publicPermissions: { policies: {}, integrationPolicies: {}, resourcePolicies: {} } })
const form = reactive({ personalSettingsEnabled: false, permissions: emptyPermissions(), configuration: {
  toolAvailability: {}, shellPrefixes: null,
  webSearch: { primaryProvider: 'no_key' },
  webfetch: { primaryProvider: 'builtin' }
} })
const hasOverrides = computed(() => ['privatePermissions', 'publicPermissions'].some(branch => Object.keys(form.permissions[branch].policies).length))
const payload = () => ({
  ...form.permissions,
  ...form.configuration,
  ...(!selectedId.value ? {
    toolPermissionPresets: Object.fromEntries(audiences.value.map(audience => [audience.value, selectedPresets.value[audience.value]])),
    personalSettingsEnabled: form.personalSettingsEnabled
  } : {})
})
const dirty = computed(() => Boolean(saved.value) && JSON.stringify(payload()) !== saved.value)
const loading = computed(() => tools.loading || tools.configurationLoading || permissionsLoading.value || agents.loading)

async function load() {
  section.value = initialSection()
  loadFailed.value = false
  selectedId.value = route.query.profileId || null
  try {
    await agents.fetchAgents(admin ? 'instance' : 'personal')
    if (!profileOptions.value.some(item => item.value === selectedId.value)) selectedId.value = profileOptions.value[0]?.value || null
    await loadPermissions()
  } catch { loadFailed.value = true }
}
async function loadPermissions() {
  if (!canEdit.value) return
  const token = ++loadToken
  permissionsLoading.value = true
  loadFailed.value = false
  try {
    const data = selectedId.value ? await tools.fetchProfilePermissions(selectedId.value, admin ? 'instance' : 'personal') : await tools.fetchConfiguration()
    if (token !== loadToken) return
    selectedPresets.value = data.toolPermissionPresets || { private: 'ask_changes', public: 'balanced' }
    form.permissions = { privatePermissions: data.privatePermissions || emptyPermissions().privatePermissions, publicPermissions: data.publicPermissions || emptyPermissions().publicPermissions }
    permissionGroups.value = data.groupsByTrust || { private: data.privateGroups || [], public: data.publicGroups || [] }
    form.configuration = { toolAvailability: { ...data.toolAvailability }, shellPrefixes: data.shellPrefixes ?? null }
    inheritedShellPrefixes.value = data.inheritedShellPrefixes || { read: [], write: [] }
    inheritedAvailability.value = data.inheritedToolAvailability || {}
    globalAvailability.value = data.globalToolAvailability || {}
    if (!selectedId.value) {
      form.personalSettingsEnabled = data.personalSettingsEnabled
      technicalGroups.value = tools.configurationGroups
      Object.assign(form.configuration, { shellPrefixes: data.shellPrefixes || { read: [], write: [] }, webAccess: { ...tools.configurationWebAccess }, ...Object.fromEntries(mediaFields.map(field => [field, { ...tools[field] }])) })
      form.configuration.webSearch = { primaryProvider: 'no_key', ...data.webSearch }
      form.configuration.webfetch = { primaryProvider: 'builtin', ...data.webfetch }
      webProviderOptions.value = {
        webSearch: data.webSearchProviderOptions || [{ value: 'no_key', name: 'Websuche ohne API-Key' }],
        webfetch: data.webfetchProviderOptions || [{ value: 'builtin', name: 'Eingebaut' }]
      }
    }
    saved.value = JSON.stringify(payload())
  } catch { if (token === loadToken) loadFailed.value = true }
  finally { if (token === loadToken) permissionsLoading.value = false }
}
async function selectProfile(id) {
  if (!await confirmDiscardedChanges()) return
  selectedId.value = id
  await loadPermissions()
}
async function selectSection(value) {
  if (saving.value || value === section.value) return
  if (value === 'general' && selectedId.value) {
    if (!await confirmDiscardedChanges()) return
    selectedId.value = null
    await loadPermissions()
  }
  section.value = value
}
async function resetPermissions() {
  if (!await confirm('Alle eigenen Regeln dieses Profils entfernen, einschließlich öffentlicher Werte? Die Tool-Einstellungen bleiben erhalten.', 'Alle eigenen Regeln entfernen?', { okLabel: 'Entfernen', okColor: 'negative' })) return
  form.permissions = emptyPermissions()
}
async function save() {
  if (!canEdit.value || loading.value || saving.value || loadFailed.value || !await formRef.value?.validate()) return
  saving.value = true
  try {
    if (selectedId.value) await tools.saveProfilePermissions(selectedId.value, admin ? 'instance' : 'personal', payload())
    else await tools.saveConfiguration(payload())
    await loadPermissions()
    $q.notify({ type: 'positive', message: 'Tool-Einstellungen gespeichert' })
  } catch { /* Central API toast. */ }
  finally { saving.value = false }
}
async function confirmDiscardedChanges() {
  if (saving.value) return false
  if (!dirty.value) return true
  return confirm('Sollen die ungespeicherten Änderungen verworfen werden?', 'Änderungen verwerfen?', { okLabel: 'Verwerfen' })
}
onMounted(load)
watch(() => [admin, route.query.profileId, route.query.tab], load)
onBeforeRouteLeave(confirmDiscardedChanges)
</script>
