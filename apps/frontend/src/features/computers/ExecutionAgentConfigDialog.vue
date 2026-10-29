<template>
  <q-dialog
    :transition-duration="0"
    :model-value="modelValue"
    :maximized="$q.screen.lt.sm"
    :persistent="blocked"
    @hide="close"
  >
    <q-card class="execution-agent-dialog app-surface column no-wrap">
      <q-card-section class="row items-start no-wrap q-gutter-md">
        <q-avatar color="primary" text-color="white" icon="computer" />
        <div class="col">
          <div class="text-h6">{{ isEditing ? 'Computer verwalten' : 'Computer hinzufügen' }}</div>
          <div class="text-body2 app-muted">
            {{ isEditing ? agentDisplayName(agent) : 'Computer anlegen und anschließend koppeln.' }}
          </div>
        </div>
        <q-btn
          flat
          round
          dense
          icon="close"
          aria-label="Dialog schließen"
          :disable="blocked"
          @click="close"
        />
      </q-card-section>

      <q-separator />

      <template v-if="isEditing">
        <q-tabs v-model="tab" dense no-caps align="left" active-color="primary" indicator-color="primary">
          <q-tab name="overview" label="Übersicht" :disable="blocked" />
          <q-tab name="settings" label="Einstellungen" :disable="blocked" />
          <q-tab name="actions" label="Aktionen" :disable="blocked" />
        </q-tabs>
        <q-separator />
      </template>

      <q-form ref="formRef" class="col column no-wrap" @submit.prevent="save">
        <q-tab-panels v-model="tab" keep-alive class="col bg-transparent">
          <q-tab-panel v-if="isEditing" name="overview" class="q-pa-none">
            <q-card-section class="q-gutter-y-md">
              <slot name="overview" />
            </q-card-section>
          </q-tab-panel>
          <q-tab-panel name="settings" class="q-pa-none">
            <q-card-section class="q-gutter-md">
              <template v-if="!isEditing">
                <div class="text-subtitle2">Agent installieren</div>
                <div class="row items-center" role="group" aria-label="Betriebssystem auswählen">
                  <q-chip
                    v-for="platform in installationPlatforms"
                    :key="platform.value"
                    clickable
                    :selected="installationPlatform === platform.value"
                    :icon="platform.icon"
                    :icon-selected="platform.icon"
                    :label="platform.label"
                    :color="installationPlatform === platform.value ? 'primary' : undefined"
                    :text-color="installationPlatform === platform.value ? 'white' : undefined"
                    role="button"
                    :aria-pressed="installationPlatform === platform.value"
                    @click="installationPlatform = platform.value"
                    @keydown.space.prevent
                    @keyup.space.prevent="installationPlatform = platform.value"
                  />
                </div>
                <div class="text-body2 app-muted">
                  {{ installationPlatform === 'windows'
                    ? 'PowerShell als Administrator unter dem späteren Task-Benutzer öffnen. Für den Start beim Hochfahren wird dessen Passwort einmalig abgefragt.'
                    : 'Auf dem Zielcomputer als normaler Benutzer ausführen.' }}
                </div>
                <q-input :model-value="installationCommand" readonly outlined dense type="textarea" autogrow label="Installationsbefehl">
                  <template #append>
                    <q-btn flat round dense icon="content_copy" aria-label="Installationsbefehl kopieren" @click="emit('copy-command', installationCommand)" />
                  </template>
                </q-input>
                <q-separator />
              </template>
              <q-input dense
                v-model="alias"
                :autofocus="isEditing"
                outlined
                label="Alias (optional)"
                hint="Leer lassen, um den Computernamen zu verwenden."
                maxlength="160"
              />

              <AppSelect dense
                v-for="field in policyFields"
                :key="field.key"
                v-model="settings[field.key]"
                outlined
                emit-value
                map-options
                :options="policyOptions"
                :label="field.label"
              />

              <q-toggle dense
                v-for="field in shellFields"
                :key="field.key"
                v-model="settings[field.key]"
                color="primary"
                :label="field.label"
              />

              <template v-if="isEditing">
                <q-checkbox dense
                  v-model="useAsDefault"
                  color="primary"
                  label="Als Standardcomputer verwenden"
                />
                <div class="text-body2 app-muted">
                  Optional. Wird für neue Sessions vorausgewählt und ersetzt den bisherigen Standardcomputer.
                </div>
              </template>
              <AppSelect dense
                v-else
                v-model="pairingDurationSeconds"
                outlined
                emit-value
                map-options
                :options="pairingDurationOptions"
                label="Gültigkeitsdauer des Kopplungstokens"
              />
              <q-separator />
              <AppSelect
                v-model="autoUpdateEnabled" dense outlined emit-value map-options stack-label
                :display-value="autoUpdateEnabled === null ? autoUpdateOptions[0].label : undefined"
                label="Automatische Agent-Updates" :options="autoUpdateOptions"
              />
              <AppSelect
                v-model="sudoCredentialId" dense outlined emit-value map-options stack-label
                label="sudo-Passwort" :options="sudoOptions" :loading="variables.loading"
                :disable="blocked || variables.loading || variables.loadFailed"
                :display-value="sudoOptions.find(option => option.value === sudoCredentialId)?.label || 'Secret nicht mehr verfügbar'"
                hint="Vorhandenes Secret für Linux-Updates und berechtigte Admin-Ausführungen. Verwaltung unter Variablen & Secrets."
              />
              <q-btn v-if="variables.loadFailed" flat no-caps label="Secrets erneut laden" @click="loadSecrets" />
            </q-card-section>
          </q-tab-panel>
          <q-tab-panel v-if="isEditing" name="actions" class="q-pa-none">
            <q-card-section class="q-gutter-y-md">
              <slot name="actions" />
            </q-card-section>
          </q-tab-panel>
        </q-tab-panels>

        <q-separator />
        <q-card-actions class="q-pa-md">
          <q-btn flat no-caps :label="tab === 'settings' ? 'Abbrechen' : 'Schließen'" :disable="blocked" @click="close" />
          <q-space />
          <q-btn
            v-if="tab === 'settings'"
            unelevated
            no-caps
            color="primary"
            :label="isEditing ? 'Änderungen speichern' : 'Computer hinzufügen'"
            type="submit"
            :loading="saving"
            :disable="busy || (!isEditing && pairingDurationSeconds === null)"
          />
        </q-card-actions>
      </q-form>
    </q-card>
  </q-dialog>
</template>

<script setup>
import { fabLinux, fabWindows } from '@quasar/extras/fontawesome-v6'
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { agentDisplayName } from 'src/features/computers/display'
import { computed, reactive, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useExecutionAgentStore } from 'stores/execution-agents'
import { usePersonalVariableStore } from 'src/features/personal-variables/store'

defineOptions({ name: 'ExecutionAgentConfigDialog' })

const props = defineProps({
  modelValue: { type: Boolean, required: true },
  agent: { type: Object, default: null },
  busy: Boolean
})
const emit = defineEmits(['update:modelValue', 'created', 'copy-command'])

const $q = useQuasar()
const agents = useExecutionAgentStore()
const variables = usePersonalVariableStore()
const formRef = ref(null)
const saving = ref(false)
const blocked = computed(() => saving.value || props.busy)
const tab = ref('settings')
const installationPlatform = ref('linux')
const installationPlatforms = [
  { label: 'Linux', value: 'linux', icon: fabLinux },
  { label: 'Windows', value: 'windows', icon: fabWindows }
]
const installationCommand = computed(() => installationPlatform.value === 'windows'
  ? `$installer = Join-Path $env:TEMP 'install-matoloop-agent.ps1'; Invoke-WebRequest -UseBasicParsing -Uri '${window.location.origin}/api/execution-agents/install.ps1' -OutFile $installer; powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer -InstanceUrl '${window.location.origin}'`
  : `curl -fsSL ${window.location.origin}/api/execution-agents/install.sh | sh -s -- --instance-url ${window.location.origin}`)
const alias = ref('')
const autoUpdateEnabled = ref(null)
const sudoCredentialId = ref(null)
const sudoOptions = computed(() => [
  { label: 'Kein Secret zugeordnet', value: null },
  ...variables.variables.filter(variable => variable.secret).map(variable => ({ label: variable.name, value: variable.id }))
])
const autoUpdateOptions = computed(() => [
  { label: `Instanzstandard (aktuell ${agents.autoUpdateDefault ? 'an' : 'aus'})`, value: null },
  { label: 'An', value: true },
  { label: 'Aus', value: false }
])
const settings = reactive({
  homeFilesystemPolicy: 'auto',
  externalFilesystemPolicy: 'ask',
  homeShellEnabled: true,
  externalShellEnabled: true
})
const useAsDefault = ref(false)
const initialDefault = ref(false)
const pairingDurationSeconds = ref(null)
const isEditing = computed(() => Boolean(props.agent))
const isCurrentDefault = computed(() => props.agent?.id === agents.defaultExecutionAgentId)
const policyOptions = [
  { label: 'Automatisch', value: 'auto' },
  { label: 'Nachfragen', value: 'ask' },
  { label: 'Verboten', value: 'deny' }
]
const policyFields = [
  { key: 'homeFilesystemPolicy', label: 'Dateizugriff im Home-Verzeichnis' },
  { key: 'externalFilesystemPolicy', label: 'Dateizugriff außerhalb des Home-Verzeichnisses' }
]
const shellFields = [
  { key: 'homeShellEnabled', label: 'Shell im Home-Verzeichnis aktivieren' },
  { key: 'externalShellEnabled', label: 'Shell außerhalb des Home-Verzeichnisses aktivieren' }
]
const pairingDurationOptions = computed(() => [
  { label: '30 Minuten', value: 1800 },
  { label: '1 Stunde', value: 3600 },
  { label: '4 Stunden', value: 14_400 },
  { label: '12 Stunden', value: 43_200 },
  { label: '24 Stunden', value: 86_400 }
].filter((option) => option.value <= (agents.pairingPolicy?.maximumDurationSeconds || 0)))
let initialization = 0

async function save() {
  if (
    blocked.value || tab.value !== 'settings'
    || (!isEditing.value && pairingDurationSeconds.value === null)
    || !await formRef.value?.validate()
  ) return
  const agent = props.agent
  const values = { alias: alias.value.trim(), ...settings, autoUpdateEnabled: autoUpdateEnabled.value }
  if (sudoCredentialId.value !== (agent?.sudoCredentialId ?? null)) values.sudoCredentialId = sudoCredentialId.value
  const submittedDefault = useAsDefault.value
  saving.value = true
  try {
    if (!agent) {
      const result = await agents.create(values, pairingDurationSeconds.value)
      emit('update:modelValue', false)
      emit('created', result)
      return
    }
    if ([...policyFields, ...shellFields].some(({ key }) => values[key] !== agent[key]) || values.alias !== agent.alias
      || values.autoUpdateEnabled !== (agent.autoUpdateEnabled ?? null) || Object.hasOwn(values, 'sudoCredentialId')) {
      await agents.update(agent.id, values)
    }
    if (submittedDefault !== initialDefault.value) {
      await agents.setDefault(submittedDefault ? agent.id : null)
    }
    $q.notify({ type: 'positive', message: 'Computer gespeichert' })
    emit('update:modelValue', false)
  } finally {
    saving.value = false
  }
}

function close() {
  if (blocked.value) return
  emit('update:modelValue', false)
}

async function loadSecrets() {
  try { await variables.fetchVariables() } catch { /* Central API toast. */ }
}

watch(() => props.modelValue, async (open) => {
  const currentInitialization = ++initialization
  if (!open) return
  tab.value = props.agent ? 'overview' : 'settings'
  installationPlatform.value = 'linux'
  pairingDurationSeconds.value = null
  alias.value = props.agent?.alias || ''
  autoUpdateEnabled.value = props.agent?.autoUpdateEnabled ?? null
  sudoCredentialId.value = props.agent?.sudoCredentialId ?? null
  void loadSecrets()
  for (const { key } of [...policyFields, ...shellFields]) {
    settings[key] = props.agent?.[key] ?? (key.endsWith('Enabled') ? true : key === 'homeFilesystemPolicy' ? 'auto' : 'ask')
  }
  initialDefault.value = isCurrentDefault.value
  useAsDefault.value = initialDefault.value
  if (!props.agent) {
    try {
      const policy = await agents.fetchPairingPolicy()
      if (currentInitialization !== initialization) return
      pairingDurationSeconds.value = policy.defaultDurationSeconds
    } catch {
      if (currentInitialization === initialization && props.modelValue && !props.agent) close()
    }
  }
})
</script>
