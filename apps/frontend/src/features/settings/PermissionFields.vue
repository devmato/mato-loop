<template>
  <q-card v-for="group in groups" :key="group.id" flat class="app-surface q-mb-md">
    <q-card-section class="row items-center q-gutter-md">
      <q-icon :name="group.icon || 'security'" color="primary" size="sm" />
      <div class="text-subtitle1 text-weight-bold">{{ group.label }}</div>
    </q-card-section>
    <template v-for="section in group.sections" :key="section.name">
      <template v-if="section.label">
        <q-separator />
        <q-card-section class="row items-center q-gutter-sm">
          <q-icon :name="section.icon || 'build'" color="primary" size="xs" />
          <div class="text-subtitle2 text-weight-bold">{{ section.label }}</div>
        </q-card-section>
      </template>
    <template v-for="action in section.actions" :key="action.capability">
      <q-separator />
      <q-card-section class="row items-start q-col-gutter-md" :class="section.label ? 'q-pl-lg' : ''">
        <div class="col-12 col-md">
          <div class="text-weight-medium">{{ action.label }}</div>
          <div class="text-caption app-muted">{{ action.capability }}</div>
          <div v-if="toolHierarchy && action.shared" class="text-caption app-muted">Gemeinsames Aktionsrecht für mehrere Tools.</div>
          <div v-if="action.available === false" class="text-caption text-warning">Technisch nicht verfügbar</div>
        </div>
        <div v-for="audience in audiences" :key="audience.value" class="col-12 col-sm-6 col-md-3">
          <slot name="policy" :action="action" :audience="audience" :tool-name="section.name" />
        </div>
      </q-card-section>
    </template>
    </template>
  </q-card>
  <q-expansion-item v-for="resource in resources" :key="resource.key" :duration="0" :label="resource.label" icon="folder_open" class="app-surface q-mb-md">
    <template #header>
      <q-item-section avatar><q-icon name="folder_open" /></q-item-section>
      <q-item-section>
        <q-item-label class="settings-list-name">{{ resource.label }}</q-item-label>
        <q-item-label v-if="resource.toolName && resource.toolName !== resource.label" caption class="settings-list-name">{{ resource.toolName }}</q-item-label>
      </q-item-section>
      <q-item-section v-if="$slots['resource-controls']" side @click.stop @keydown.stop><slot name="resource-controls" :resource="resource" /></q-item-section>
    </template>
    <q-card-section v-if="resource.description || resource.hints?.length" class="text-body2 app-muted">
      <div v-if="resource.description" class="settings-list-name">{{ resource.description }}</div>
      <div v-if="resource.hints?.length" class="text-caption">{{ resource.hints.join(' · ') }}</div>
    </q-card-section>
    <div v-if="resourceOverridesOnly && !resourceActions(resource).length" class="text-caption app-muted q-pa-md">Keine eigenen Regeln. Es gelten die Vorgaben.</div>
    <q-card-section v-for="action in resourceActions(resource)" :key="action.capability" class="row q-col-gutter-md">
      <div class="col-12 col-md">
        <div class="text-weight-medium">{{ action.label }}</div>
        <div v-if="action.available === false || resource.available === false || resource.hardUnavailableCapabilities?.includes(action.capability)" class="text-caption text-warning">Technisch nicht verfügbar</div>
      </div>
      <div v-for="audience in audiences" :key="audience.value" class="col-12 col-sm-6 col-md-3">
        <slot name="policy" :action="action" :audience="audience" :resource="resource" />
      </div>
    </q-card-section>
  </q-expansion-item>
</template>

<script setup>
import { computed } from 'vue'

const permissions = defineModel({ type: Object, required: true })
const props = defineProps({ groupsByTrust: { type: Object, required: true }, resources: { type: Array, default: () => [] }, modelTrustEnabled: { type: Boolean, default: true }, overridesOnly: Boolean, resourceOverridesOnly: Boolean, toolHierarchy: Boolean })
const audiences = computed(() => props.modelTrustEnabled
  ? [{ value: 'private', label: 'Private Modelle' }, { value: 'public', label: 'Öffentliche Modelle' }]
  : [{ value: 'private', label: 'Alle Modelle' }])
const branch = (trust) => permissions.value[`${trust}Permissions`]
const catalogGroups = computed(() => {
  const seen = new Set()
  const owners = new Map()
  for (const group of props.groupsByTrust.private || []) for (const tool of group.tools || []) for (const action of tool.actions || []) {
    if (!owners.has(action.capability)) owners.set(action.capability, new Set())
    owners.get(action.capability).add(tool.name)
  }
  return (props.groupsByTrust.private || []).map((group) => {
    const sections = [{ name: '', actions: group.actions || [] }, ...(group.tools || []).map((tool) => ({ ...tool, actions: (tool.actions || []).map((action) => ({ ...action, toolName: tool.name, label: props.toolHierarchy ? action.label : `${tool.label}: ${action.label}`, available: tool.available !== false && action.available !== false })) }))]
    return { ...group, sections: (props.toolHierarchy ? sections : [{ name: '', actions: sections.flatMap(section => section.actions) }]).map(section => ({ ...section, actions: section.actions.filter(action => {
      if (action.configurable === false || (!props.toolHierarchy && seen.has(action.capability))) return false
      seen.add(action.capability)
      return true
    }).map(action => ({ ...action, shared: owners.get(action.capability)?.size > 1 })) })) }
  })
})
const groups = computed(() => catalogGroups.value.map(group => ({ ...group, sections: group.sections.map(section => ({ ...section, actions: section.actions.filter(action => !props.overridesOnly || audiences.value.some(({ value }) => branch(value).policies[action.capability] != null)) })).filter(section => section.actions.length) })).filter(group => group.sections.length))
const resourceActions = (resource) => catalogGroups.value.flatMap(group => group.sections.flatMap(section => section.actions)).filter((action) => (!resource.applicableCapabilities || resource.applicableCapabilities.includes(action.capability)) && (!props.resourceOverridesOnly || audiences.value.some(({ value }) => branch(value).resourcePolicies?.[resource.key]?.[action.capability] != null)))
</script>
