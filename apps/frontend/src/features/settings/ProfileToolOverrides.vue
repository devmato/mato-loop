<template>
  <div class="row items-center justify-end q-gutter-sm q-mb-md">
    <slot name="actions" />
    <q-btn flat no-caps color="primary" icon="add" label="Eigene Regel hinzufügen" :disable="disabled || !availableActions.length" @click="edit()" />
  </div>
  <q-card v-if="!overrides.length" flat class="app-surface q-mb-md">
    <q-card-section class="app-muted">Keine eigenen Regeln. Es gelten die Vorgaben.</q-card-section>
  </q-card>
  <PermissionFields v-model="permissions" :groups-by-trust="groupsByTrust" :model-trust-enabled="modelTrustEnabled" tool-hierarchy overrides-only>
    <template #policy="{ action, audience, toolName }">
      <div class="text-caption app-muted">{{ audience.label }}</div>
      <div class="row items-center no-wrap">
        <template v-if="branch(audience.value).policies[action.capability] != null">
          <div class="col">{{ labels[branch(audience.value).policies[action.capability]] }}</div>
          <q-btn flat round dense icon="edit" :aria-label="`${action.label}: ${audience.label} bearbeiten`" :disable="disabled" @click="edit(action.capability, audience.value, toolName)" />
          <q-btn flat round dense color="negative" icon="delete_outline" :aria-label="`${action.label}: ${audience.label} entfernen`" :disable="disabled" @click="remove(action.capability, audience)" />
        </template>
        <template v-else>
          <div class="col app-muted">Keine eigene Regel</div>
          <q-btn flat round dense icon="add" :aria-label="`${action.label}: ${audience.label} hinzufügen`" :disable="disabled" @click="edit(action.capability, audience.value, toolName)" />
        </template>
      </div>
    </template>
  </PermissionFields>

  <q-dialog v-model="dialogOpen" :transition-duration="0" :maximized="$q.screen.lt.sm" aria-labelledby="permission-override-title">
    <q-card class="app-surface column no-wrap full-width">
      <q-card-section class="row items-center">
        <div id="permission-override-title" class="text-h6 col">Eigene Regel {{ editing ? 'bearbeiten' : 'hinzufügen' }}</div>
        <q-btn v-close-popup flat round dense icon="close" aria-label="Dialog schließen" />
      </q-card-section>
      <q-card-section class="col scroll q-gutter-md">
          <AppSelect :model-value="selectedTool" outlined emit-value map-options use-input :input-debounce="0" label="Tool oder Bereich" :options="toolOptions" :disable="editing || disabled" @filter="filterTools" @update:model-value="selectTool" />
          <AppSelect v-if="selectedTool" v-model="draft.capability" outlined emit-value map-options label="Aktion" :options="actionOptions" :disable="editing || disabled" />
          <div v-if="sharedAction" class="text-caption app-muted">Diese Regel gilt auch für weitere Tools, die dieselbe Aktion verwenden.</div>
        <template v-if="draft.capability">
          <AppSelect v-if="modelTrustEnabled" v-model="draft.trust" outlined emit-value map-options label="Modellbereich" :options="audiences.map(audience => ({ ...audience, disable: !editing && branch(audience.value).policies[draft.capability] != null }))" :disable="disabled || editing" />
          <AppSelect v-model="draft.policy" outlined emit-value map-options label="Berechtigung" :options="policyOptions" :disable="disabled" />
        </template>
        <div class="text-caption app-muted">Mit „Übernehmen“ wird das Formular aktualisiert. Anschließend die Einstellungen auf der Seite speichern.</div>
      </q-card-section>
      <q-card-actions align="right">
        <q-btn v-close-popup flat no-caps label="Abbrechen" />
        <q-btn unelevated no-caps color="primary" label="Übernehmen" :disable="disabled || !draft.capability" @click="apply" />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import AppSelect from 'src/shared/ui/AppSelect.vue'
import PermissionFields from './PermissionFields.vue'
import { useConfirm } from 'src/composables/confirm'
import { setPolicyOverride } from 'src/utils/agent-permissions'

const permissions = defineModel({ type: Object, required: true })
const props = defineProps({ groupsByTrust: { type: Object, required: true }, modelTrustEnabled: { type: Boolean, default: true }, disabled: Boolean })
const $q = useQuasar()
const { confirm } = useConfirm()
const labels = { auto: 'Automatisch', ask: 'Nachfragen', disabled: 'Deaktiviert' }
const audiences = computed(() => props.modelTrustEnabled
  ? [{ value: 'private', label: 'Private Modelle' }, { value: 'public', label: 'Öffentliche Modelle' }]
  : [{ value: 'private', label: 'Alle Modelle' }])
const branch = (trust) => permissions.value[`${trust}Permissions`]
const catalog = computed(() => Object.fromEntries(['private', 'public'].map((trust) => {
  const actions = {}
  for (const group of props.groupsByTrust[trust] || []) {
    for (const action of [
      ...(group.actions || []),
      ...(group.tools || []).flatMap(tool => tool.actions || [])
    ]) {
      if (action.configurable === false || actions[action.capability]) continue
      actions[action.capability] = { ...action, label: `${group.label}: ${action.label}` }
    }
  }
  return [trust, actions]
})))
const actionLabel = (capability) => catalog.value.private[capability]?.label || capability
const overrides = computed(() => [...new Set(audiences.value.flatMap(({ value }) => Object.keys(branch(value).policies)))])
const availableActions = computed(() => Object.values(catalog.value.private).filter(action => audiences.value.some(({ value }) => branch(value).policies[action.capability] == null)))
const dialogOpen = ref(false)
const editing = ref(false)
const search = ref('')
const selectedTool = ref(null)
const draft = ref({ capability: null, trust: 'private', policy: 'ask' })
const toolEntries = computed(() => (props.groupsByTrust.private || []).flatMap(group => [
  { value: `group:${group.id}`, label: `${group.label}: Zusatzrechte`, caption: group.label, actions: group.actions || [] },
  ...(group.tools || []).map(tool => ({ value: tool.name, label: tool.label, caption: group.label, actions: tool.actions || [] }))
]).map(tool => ({ ...tool, actions: tool.actions.filter(action => action.configurable !== false) })).filter(tool => tool.actions.length))
const canAdd = action => availableActions.value.some(item => item.capability === action.capability)
const toolOptions = computed(() => toolEntries.value.filter(tool => (editing.value || tool.actions.some(canAdd)) && `${tool.label} ${tool.caption} ${tool.value}`.toLocaleLowerCase().includes(search.value.toLocaleLowerCase())))
const actionOptions = computed(() => (toolEntries.value.find(tool => tool.value === selectedTool.value)?.actions || [])
  .filter(action => editing.value || canAdd(action))
  .map(action => ({ label: action.label, value: action.capability, caption: action.capability })))
const sharedAction = computed(() => Boolean(draft.value.capability) && (props.groupsByTrust.private || []).flatMap(group => group.tools || []).filter(tool => tool.actions?.some(action => action.capability === draft.value.capability)).length > 1)

function filterTools(value, update) {
  update(() => { search.value = value })
}

function selectTool(value) {
  selectedTool.value = value
  draft.value.capability = actionOptions.value.length === 1 ? actionOptions.value[0].value : null
}

const policyOptions = computed(() => (catalog.value[draft.value.trust][draft.value.capability]?.allowedPolicies || ['disabled', 'ask', 'auto']).map(value => ({ label: labels[value], value })))

function edit(capability = null, trust = 'private', toolName = null) {
  search.value = ''
  editing.value = branch(trust).policies[capability] != null
  selectedTool.value = toolName || toolEntries.value.find(tool => tool.actions.some(action => action.capability === capability))?.value || null
  draft.value = { capability, trust, policy: branch(trust).policies[capability] || catalog.value[trust][capability]?.policy || 'ask' }
  dialogOpen.value = true
}

function apply() {
  setPolicyOverride(branch(draft.value.trust).policies, [draft.value.capability], draft.value.policy)
  dialogOpen.value = false
}

async function remove(capability, audience) {
  if (!await confirm(`Eigene Regel „${actionLabel(capability)}“ für „${audience.label}“ entfernen? Danach gilt wieder die geerbte Vorgabe.`, 'Eigene Regel entfernen?', { okLabel: 'Entfernen' })) return
  setPolicyOverride(branch(audience.value).policies, [capability], 'inherit')
}

watch(() => [draft.value.capability, draft.value.trust], ([capability, trust]) => {
  if (!capability) return
  if (!editing.value && branch(trust).policies[capability] != null) {
    draft.value.trust = audiences.value.find(({ value }) => branch(value).policies[capability] == null)?.value || trust
  }
  draft.value.policy = branch(draft.value.trust).policies[capability] || catalog.value[draft.value.trust][capability]?.policy || 'ask'
})
</script>
