<template>
  <div class="row justify-end q-mb-md">
    <q-btn flat no-caps color="primary" icon="add" label="Eigene Einstellung hinzufügen" :disable="disabled || !availableTools.length" @click="edit()" />
  </div>
  <q-card v-if="!visibleGroups.length" flat class="app-surface">
    <q-card-section class="app-muted">Keine eigenen Einstellungen. Es gelten die Vorgaben.</q-card-section>
  </q-card>
  <q-card v-for="group in visibleGroups" :key="group.id" flat class="app-surface q-mb-md">
    <q-card-section class="row items-center q-gutter-md">
      <q-icon :name="group.icon || 'build'" color="primary" size="sm" />
      <div class="text-subtitle1 text-weight-bold">{{ group.label }}</div>
    </q-card-section>
    <template v-for="tool in group.tools" :key="tool.name">
      <q-separator />
      <q-card-section class="row items-center q-gutter-sm">
        <q-icon :name="tool.icon || 'build'" color="primary" size="xs" />
        <div class="text-subtitle2 text-weight-bold">{{ tool.label }}</div>
      </q-card-section>
      <q-card-section v-for="setting in tool.settings" :key="setting.key" class="row items-center q-col-gutter-md q-pl-lg">
        <div class="col-12 col-sm">
          <div class="text-weight-medium">{{ setting.label }}</div>
          <div v-if="setting.key === 'availability' && globallyDisabled(tool.name)" class="text-caption text-warning">Global deaktiviert</div>
        </div>
        <div class="col row items-center no-wrap">
          <div class="col">{{ summary(tool.name, setting.key) }}</div>
          <q-btn flat round dense icon="edit" :aria-label="`${tool.label}: ${setting.label} bearbeiten`" :disable="disabled || (setting.key === 'availability' && globallyDisabled(tool.name))" @click="edit(tool.name, setting.key)" />
          <q-btn flat round dense color="negative" icon="delete_outline" :aria-label="`${tool.label}: ${setting.label} entfernen`" :disable="disabled" @click="remove(tool, setting)" />
        </div>
      </q-card-section>
    </template>
  </q-card>

  <q-dialog v-model="open" :transition-duration="0" :maximized="$q.screen.lt.sm" aria-labelledby="tool-setting-title">
    <q-card class="app-surface column no-wrap full-width">
      <q-card-section class="row items-center">
        <div id="tool-setting-title" class="text-h6 col">Eigene Einstellung {{ editing ? 'bearbeiten' : 'hinzufügen' }}</div>
        <q-btn v-close-popup flat round dense icon="close" aria-label="Dialog schließen" />
      </q-card-section>
      <q-card-section class="col scroll">
        <q-form ref="formRef" class="q-gutter-md" @submit.prevent="apply">
          <AppSelect v-model="draft.tool" outlined emit-value map-options use-input :input-debounce="0" label="Tool" :options="toolOptions" :disable="disabled || editing" @filter="filterTools" @update:model-value="selectTool" />
          <AppSelect v-if="draft.tool" v-model="draft.setting" outlined emit-value map-options label="Einstellung" :options="settingOptions" :disable="disabled || editing" @update:model-value="loadDraft" />
          <template v-if="draft.setting">
            <q-toggle v-if="draft.setting === 'availability'" v-model="draft.value" color="primary" label="Tool aktiviert" :disable="disabled || globallyDisabled(draft.tool)" />
            <div v-if="draft.setting === 'availability' && globallyDisabled(draft.tool)" class="text-caption text-warning">Global deaktiviert. Dieses Profil kann das Tool nicht aktivieren.</div>
            <div v-if="draft.setting === 'shellPrefixes'"><ShellPrefixFields v-model="shellDraft" :disabled="disabled" /></div>
          </template>
          <div class="text-caption app-muted">Mit „Übernehmen“ wird das Formular aktualisiert. Anschließend die Einstellungen auf der Seite speichern.</div>
        </q-form>
      </q-card-section>
      <q-card-actions align="right">
        <q-btn v-close-popup flat no-caps label="Abbrechen" />
        <q-btn unelevated no-caps color="primary" label="Übernehmen" :disable="disabled || !draft.setting" @click="apply" />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useQuasar } from 'quasar'
import AppSelect from 'src/shared/ui/AppSelect.vue'
import ShellPrefixFields from './ShellPrefixFields.vue'
import { useConfirm } from 'src/composables/confirm'

const configuration = defineModel({ type: Object, required: true })
const props = defineProps({ groups: { type: Array, default: () => [] }, inheritedShellPrefixes: { type: Object, default: () => ({ read: [], write: [] }) }, inheritedAvailability: { type: Object, default: () => ({}) }, globalAvailability: { type: Object, default: () => ({}) }, disabled: Boolean })
const $q = useQuasar()
const { confirm } = useConfirm()
const settingsFor = tool => [
  { key: 'availability', label: 'Verfügbarkeit' },
  ...(tool === 'shell_run' ? [{ key: 'shellPrefixes', label: 'Shell-Präfixe' }] : [])
]
const own = (tool, key) => {
  if (key === 'availability') return Object.hasOwn(configuration.value.toolAvailability, tool)
  return configuration.value.shellPrefixes != null
}
const globallyDisabled = tool => props.globalAvailability[tool] === false
const visibleGroups = computed(() => props.groups.map(group => ({ ...group, tools: (group.tools || []).map(tool => ({ ...tool, settings: settingsFor(tool.name).filter(setting => own(tool.name, setting.key)) })).filter(tool => tool.settings.length) })).filter(group => group.tools.length))
const allTools = computed(() => props.groups.flatMap(group => (group.tools || []).map(tool => ({ ...tool, caption: group.label }))))
const availableTools = computed(() => allTools.value.filter(tool => settingsFor(tool.name).some(setting => !own(tool.name, setting.key))))
const open = ref(false)
const editing = ref(false)
const search = ref('')
const formRef = ref(null)
const draft = ref({ tool: null, setting: null, value: null })
const shellDraft = ref({ shellCustom: true, shellRead: '', shellWrite: '' })
const toolOptions = computed(() => (editing.value ? allTools.value : availableTools.value).filter(tool => `${tool.label} ${tool.name} ${tool.caption}`.toLocaleLowerCase().includes(search.value.toLocaleLowerCase())).map(tool => ({ label: tool.label, value: tool.name, caption: tool.caption })))
const settingOptions = computed(() => settingsFor(draft.value.tool).filter(setting => editing.value || !own(draft.value.tool, setting.key)).map(setting => ({ label: setting.label, value: setting.key })))
const prefixes = value => value.split('\n').map(line => line.trim().split(/\s+/).filter(Boolean)).filter(tokens => tokens.length)

function valueFor(tool, key) {
  if (key === 'availability') return !globallyDisabled(tool) && (configuration.value.toolAvailability[tool] ?? props.inheritedAvailability[tool] ?? true)
  return configuration.value.shellPrefixes ?? props.inheritedShellPrefixes
}

function summary(tool, key) {
  const value = valueFor(tool, key)
  if (key === 'shellPrefixes') return 'Eigene Werte'
  return value ? 'Ein' : 'Aus'
}

function filterTools(value, update) { update(() => { search.value = value }) }
function selectTool() {
  draft.value.setting = settingOptions.value.length === 1 ? settingOptions.value[0].value : null
  loadDraft()
}
function loadDraft() {
  if (!draft.value.setting) return
  draft.value.value = valueFor(draft.value.tool, draft.value.setting)
  if (draft.value.setting === 'shellPrefixes') {
    shellDraft.value = { shellCustom: true, shellRead: draft.value.value.read.map(item => item.join(' ')).join('\n'), shellWrite: draft.value.value.write.map(item => item.join(' ')).join('\n') }
  }
}
function edit(tool = null, setting = null) {
  search.value = ''
  editing.value = setting !== null
  draft.value = { tool, setting, value: null }
  loadDraft()
  open.value = true
}
async function apply() {
  if (props.disabled || !draft.value.setting || !await formRef.value.validate()) return
  const { tool, setting, value } = draft.value
  if (setting === 'availability') configuration.value.toolAvailability[tool] = value
  else configuration.value.shellPrefixes = { read: prefixes(shellDraft.value.shellRead), write: prefixes(shellDraft.value.shellWrite) }
  open.value = false
}
async function remove(tool, setting) {
  if (!await confirm(`Eigene Einstellung „${setting.label}“ für „${tool.label}“ entfernen? Danach gilt wieder die Vorgabe.`, 'Eigene Einstellung entfernen?', { okLabel: 'Entfernen' })) return
  if (setting.key === 'availability') delete configuration.value.toolAvailability[tool.name]
  else configuration.value.shellPrefixes = null
}
</script>
