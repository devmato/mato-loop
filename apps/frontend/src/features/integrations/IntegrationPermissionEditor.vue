<template>
  <div v-if="resources.some(resource => resource.toolName)" class="text-caption app-muted q-mb-md">Tool-Metadaten stammen vom Server und sind nicht verifiziert. Sie bestimmen keine Berechtigungen.</div>
  <div class="row justify-end q-gutter-sm q-mb-md">
    <q-btn v-if="profileId" flat round dense icon="delete_outline" aria-label="Alle eigenen Regeln und Einstellungen entfernen" color="negative" :disable="disabled || !hasOverrides" @click="removeAll" />
    <q-btn v-if="profileId && permissions.available == null" flat no-caps icon="tune" color="primary" label="Verfügbarkeit anpassen" :disable="disabled" @click="edit('', 'availability')" />
    <q-btn v-if="permissions.toolLoading == null" flat no-caps icon="manage_search" color="primary" label="Tool-Ladeverhalten anpassen" :disable="disabled" @click="edit('', 'tool_loading')" />
    <q-btn v-if="groups.private[0].actions.length" flat no-caps icon="add" color="primary" label="Eigene Regel hinzufügen" :disable="disabled" @click="edit()" />
  </div>
  <q-card v-if="profileId && permissions.available != null" flat class="app-surface q-mb-md">
    <q-card-section class="row items-center q-gutter-sm">
      <div class="col">Integration für dieses Profil</div>
      <q-toggle v-model="permissions.available" dense :disable="disabled" aria-label="Integration für dieses Profil verfügbar" />
      <q-btn flat round dense icon="delete_outline" aria-label="Eigene Verfügbarkeit entfernen" :disable="disabled" @click="removeAvailability()" />
    </q-card-section>
  </q-card>
  <q-card v-if="permissions.toolLoading != null" flat class="app-surface q-mb-md">
    <q-card-section class="row items-center q-gutter-sm">
      <AppSelect v-model="permissions.toolLoading" class="col" outlined emit-value map-options label="Tool-Ladeverhalten" :options="loadingOptions" :hint="`Vorgabe: ${data.inheritedToolLoading === 'all' ? 'Sofort' : 'Bei Bedarf'}. Ändert keine Zugriffsrechte.`" :disable="disabled" />
      <q-btn flat round dense icon="delete_outline" aria-label="Eigenes Tool-Ladeverhalten entfernen" :disable="disabled" @click="removeLoading()" />
    </q-card-section>
  </q-card>
  <div v-else class="text-caption app-muted q-mb-md">Tool-Ladeverhalten – Vorgabe: {{ data.inheritedToolLoading === 'all' ? 'Sofort' : 'Bei Bedarf' }}.</div>
  <q-card v-if="!hasVisibleOverrides" flat class="app-surface q-mb-md"><q-card-section class="app-muted">Keine eigenen Regeln. Es gelten die Vorgaben.</q-card-section></q-card>
  <PermissionFields v-model="permissions" :groups-by-trust="groups" :resources="visibleResources" :model-trust-enabled="modelTrustEnabled" overrides-only resource-overrides-only>
    <template #policy="{ action, audience, resource }">
      <div class="text-caption app-muted">{{ audience.label }}</div>
      <div class="row items-center no-wrap">
        <template v-if="ownPolicy(audience.value, resource?.key || '', action.capability) !== 'inherit'">
          <div class="col">{{ policyLabels[ownPolicy(audience.value, resource?.key || '', action.capability)] }}</div>
          <q-btn flat round dense icon="edit" :aria-label="`${resource?.label || 'Integration'}: ${action.label}, ${audience.label} bearbeiten`" :disable="disabled" @click="edit(resource?.key || '', action.capability, audience.value)" />
          <q-btn flat round dense color="negative" icon="delete_outline" :aria-label="`${resource?.label || 'Integration'}: ${action.label}, ${audience.label} entfernen`" :disable="disabled" @click="removePolicy(resource?.key || '', action.capability, audience.value)" />
        </template>
        <template v-else>
          <div class="col app-muted">Keine eigene Regel</div>
          <q-btn flat round dense icon="add" :aria-label="`${resource?.label || 'Integration'}: ${action.label}, ${audience.label} hinzufügen`" :disable="disabled" @click="edit(resource?.key || '', action.capability, audience.value)" />
        </template>
      </div>
    </template>
    <template #resource-controls="{ resource }">
      <div class="row items-center no-wrap">
        <q-toggle dense :model-value="resource.available" :aria-label="`${resource.label}: verfügbar`" :disable="disabled" @update:model-value="permissions.resourceAvailability[resource.key] = $event" />
        <q-btn flat round dense icon="add" :aria-label="`${resource.label}: Eigene Regel hinzufügen`" :disable="disabled" @click="edit(resource.key)" />
        <q-btn v-if="Object.hasOwn(permissions.resourceAvailability, resource.key)" flat round dense icon="undo" :aria-label="`${resource.label}: Verfügbarkeit erben`" :disable="disabled" @click="removeAvailability(resource.key)" />
      </div>
    </template>
  </PermissionFields>
  <q-dialog v-model="open" :transition-duration="0" :maximized="$q.screen.lt.sm" aria-labelledby="integration-rule-title">
    <q-card class="app-surface column no-wrap full-width">
      <q-card-section class="row items-center">
        <div id="integration-rule-title" class="text-h6 col">{{ draft.capability === 'tool_loading' ? 'Tool-Ladeverhalten anpassen' : draft.capability === 'availability' ? 'Verfügbarkeit anpassen' : editing ? 'Eigene Regel bearbeiten' : 'Eigene Regel hinzufügen' }}</div>
        <q-btn v-close-popup flat round dense icon="close" aria-label="Dialog schließen" />
      </q-card-section>
      <q-card-section class="col scroll q-gutter-md">
        <template v-if="draft.capability !== 'tool_loading' && (draft.capability !== 'availability' || draft.resource)">
          <AppSelect v-model="draft.resource" label="Bereich" outlined emit-value map-options use-input :input-debounce="0" :options="targetOptions" :disable="editing || disabled" @filter="filterTargets" @update:model-value="draft.capability = null" />
          <div v-if="selectedResource?.toolName" class="text-body2 app-muted">
            <div v-if="selectedResource.toolName !== selectedResource.label" class="text-caption settings-list-name">{{ selectedResource.toolName }}</div>
            <div v-if="selectedResource.description" class="settings-list-name">{{ selectedResource.description }}</div>
            <div v-if="selectedResource.hints?.length" class="text-caption">{{ selectedResource.hints.join(' · ') }}</div>
          </div>
          <AppSelect v-model="draft.capability" label="Aktion" outlined emit-value map-options :options="actionOptions" :disable="editing || disabled" />
        </template>
        <q-toggle v-if="draft.capability === 'availability'" v-model="draft.available" :label="draft.resource ? 'Ressource verfügbar' : 'Integration für dieses Profil verfügbar'" :disable="disabled" />
        <AppSelect v-else-if="draft.capability === 'tool_loading'" v-model="draft.toolLoading" label="Tool-Ladeverhalten" outlined emit-value map-options :options="loadingOptions" hint="Lädt nur erlaubte Werkzeuge. Berechtigungen und Freigaben bleiben unverändert." :disable="disabled" />
        <template v-else-if="draft.capability">
          <AppSelect v-if="modelTrustEnabled" v-model="draft.trust" outlined emit-value map-options label="Modellbereich" :options="audiences.map(audience => ({ ...audience, disable: !editing && ownPolicy(audience.value, draft.resource, draft.capability) !== 'inherit' }))" :disable="editing || disabled" />
          <AppSelect v-model="draft.policy" outlined emit-value map-options label="Berechtigung" :options="policyOptions" :disable="disabled" />
        </template>
        <div class="text-caption app-muted">Übernehmen aktualisiert das Formular. Anschließend die Seite speichern.</div>
      </q-card-section>
      <q-card-actions align="right"><q-btn v-close-popup flat no-caps label="Abbrechen" /><q-btn color="primary" unelevated no-caps label="Übernehmen" :disable="disabled || !draft.capability" @click="apply" /></q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useConfirm } from 'src/composables/confirm'
import AppSelect from 'src/shared/ui/AppSelect.vue'
import PermissionFields from 'src/features/settings/PermissionFields.vue'
import { policyOverride, setPolicyOverride } from 'src/utils/agent-permissions'

const permissions = defineModel({ type: Object, required: true })
const props = defineProps({ data: { type: Object, required: true }, profileId: { type: String, default: null }, modelTrustEnabled: Boolean, disabled: Boolean })
const $q = useQuasar()
const { confirm } = useConfirm()
const policyLabels = { auto: 'Automatisch', ask: 'Nachfragen', disabled: 'Deaktiviert' }
const loadingOptions = [{ label: 'Bei Bedarf', value: 'on_demand' }, { label: 'Sofort', value: 'all' }]
const audiences = computed(() => props.modelTrustEnabled ? [{ value: 'private', label: 'Private Modelle' }, { value: 'public', label: 'Öffentliche Modelle' }] : [{ value: 'private', label: 'Alle Modelle' }])
const branch = trust => permissions.value[`${trust}Permissions`]
const groups = computed(() => Object.fromEntries(['private', 'public'].map(trust => [trust, [{ id: 'integration', label: 'Berechtigungen', icon: 'security', tools: [], actions: (props.data.inherited[trust]?.actions || []).map(action => ({ ...action, available: !props.profileId || (permissions.value.available ?? props.data.inheritedAvailable) })) }]])))
const resources = computed(() => props.data.inherited.private?.resources || [])
const inheritedResources = computed(() => Object.fromEntries(['private', 'public'].map(trust => [trust, props.data.inherited[trust]?.resources || []])))
const hasOverrides = computed(() => permissions.value.toolLoading != null || permissions.value.available != null || Object.keys(permissions.value.resourceAvailability).length || ['private', 'public'].some(trust => Object.keys(branch(trust).policies).length || Object.keys(branch(trust).resourcePolicies).length))
const resourceOverridden = key => permissions.value.resourceAvailability[key] != null || audiences.value.some(({ value }) => Object.keys(branch(value).resourcePolicies[key] || {}).length)
const visibleResources = computed(() => resources.value.filter(resource => !props.profileId || resourceOverridden(resource.key)).map(resource => ({ ...resource, available: permissions.value.resourceAvailability[resource.key] ?? resource.available ?? true })))
const hasVisibleOverrides = computed(() => permissions.value.toolLoading != null || permissions.value.available != null || resources.value.some(resource => resourceOverridden(resource.key)) || audiences.value.some(({ value }) => Object.keys(branch(value).policies).length))
const open = ref(false)
const editing = ref(false)
const draft = ref({ resource: '', capability: null, available: true, trust: 'private', policy: 'ask' })
const search = ref('')
const selectedResource = computed(() => resources.value.find(resource => resource.key === draft.value.resource))
const targetOptions = computed(() => [{ label: 'Integration', value: '' }, ...resources.value.map(resource => ({ label: resource.label, value: resource.key, caption: resource.toolName !== resource.label ? resource.toolName : undefined }))]
  .filter(option => `${option.label} ${option.caption || ''}`.toLocaleLowerCase().includes(search.value.toLocaleLowerCase())))
function filterTargets(value, update) { update(() => { search.value = value }) }
const path = (resource, capability) => resource ? [resource, capability] : [capability]
const policies = (trust, resource) => resource ? branch(trust).resourcePolicies : branch(trust).policies
const ownPolicy = (trust, resource, capability) => policyOverride(policies(trust, resource), path(resource, capability))
const actionOptions = computed(() => [
  ...(draft.value.resource && permissions.value.resourceAvailability[draft.value.resource] == null ? [{ label: 'Verfügbarkeit', value: 'availability' }] : []),
  ...groups.value.private[0].actions.filter(action => (!selectedResource.value?.applicableCapabilities || selectedResource.value.applicableCapabilities.includes(action.capability)) && (editing.value || audiences.value.some(({ value }) => ownPolicy(value, draft.value.resource, action.capability) === 'inherit'))).map(action => ({ label: action.label, value: action.capability }))
])
const policyOptions = computed(() => (groups.value.private[0].actions.find(action => action.capability === draft.value.capability)?.allowedPolicies || ['disabled', 'ask', 'auto']).map(value => ({ label: policyLabels[value], value })))
function inheritedPolicy(trust) {
  const { resource, capability } = draft.value
  const action = groups.value[trust][0].actions.find(item => item.capability === capability)
  return resource ? inheritedResources.value[trust].find(item => item.key === resource)?.policies?.[capability] || branch(trust).policies[capability] || action?.policy || 'ask' : action?.policy || 'ask'
}
function edit(resource = '', capability = null, trust = 'private') {
  search.value = ''
  editing.value = Boolean(capability) && !['availability', 'tool_loading'].includes(capability) && ownPolicy(trust, resource, capability) !== 'inherit'
  draft.value = { resource, capability, trust, available: permissions.value.available ?? props.data.inheritedAvailable ?? true, toolLoading: permissions.value.toolLoading ?? props.data.inheritedToolLoading ?? 'on_demand', policy: 'ask' }
  const own = ownPolicy(trust, resource, capability)
  draft.value.policy = own === 'inherit' ? inheritedPolicy(trust) : own
  open.value = true
}
function apply() {
  if (props.disabled || !draft.value.capability) return
  const { resource, capability, available, trust, policy } = draft.value
  if (capability === 'tool_loading') permissions.value.toolLoading = draft.value.toolLoading
  else if (capability === 'availability') {
    if (resource) permissions.value.resourceAvailability[resource] = available
    else permissions.value.available = available
  }
  else setPolicyOverride(policies(trust, resource), path(resource, capability), policy)
  open.value = false
}
async function removePolicy(resource, capability, trust) {
  if (!await confirm('Diese eigene Regel entfernen und die Vorgabe wieder übernehmen?', 'Eigene Regel entfernen?', { okLabel: 'Entfernen' })) return
  setPolicyOverride(policies(trust, resource), path(resource, capability), 'inherit')
}
async function removeAvailability(resource = '') {
  if (!await confirm('Die Verfügbarkeit wieder von der Vorgabe übernehmen?', 'Eigene Einstellung entfernen?', { okLabel: 'Entfernen' })) return
  if (resource) delete permissions.value.resourceAvailability[resource]
  else permissions.value.available = null
}
async function removeLoading() {
  if (!await confirm('Das Tool-Ladeverhalten wieder von der Vorgabe übernehmen?', 'Eigene Einstellung entfernen?', { okLabel: 'Entfernen' })) return
  permissions.value.toolLoading = null
}
async function removeAll() {
  if (!await confirm('Alle eigenen Regeln und Einstellungen dieses Profils für diese Integration entfernen, einschließlich Ressourcen und öffentlicher Werte?', 'Eigene Regeln und Einstellungen entfernen?', { okLabel: 'Entfernen' })) return
  permissions.value = { available: null, resourceAvailability: {}, privatePermissions: { policies: {}, resourcePolicies: {} }, publicPermissions: { policies: {}, resourcePolicies: {} } }
}
watch(() => [draft.value.resource, draft.value.capability, draft.value.trust], ([resource, capability, trust]) => {
  if (!capability || capability === 'tool_loading') return
  if (capability === 'availability') {
    if (resource) draft.value.available = permissions.value.resourceAvailability[resource] ?? selectedResource.value?.available ?? true
    return
  }
  if (!editing.value && ownPolicy(trust, resource, capability) !== 'inherit') {
    draft.value.trust = audiences.value.find(({ value }) => ownPolicy(value, resource, capability) === 'inherit')?.value || trust
  }
  const own = ownPolicy(draft.value.trust, resource, capability)
  draft.value.policy = own === 'inherit' ? inheritedPolicy(draft.value.trust) : own
})
</script>
