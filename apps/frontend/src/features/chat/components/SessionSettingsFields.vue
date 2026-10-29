<template>
  <div :class="equalFields ? 'session-start-fields' : compact ? 'row items-center q-gutter-xs' : grouped ? 'q-gutter-y-md' : 'column q-gutter-md'">
    <slot name="before" />
    <q-banner v-if="showWarning && (warning || computerInfo)" dense rounded class="app-surface-tonal" :role="warning ? 'alert' : 'status'">
      <template #avatar><q-icon :name="warning ? 'warning_amber' : 'info_outline'" :color="warning ? 'warning' : 'info'" /></template>
      {{ warning || computerInfo }}
    </q-banner>

    <section v-if="grouped && hasSessionFields" class="session-settings-group" aria-labelledby="session-settings-title">
      <div id="session-settings-title" class="text-subtitle1 text-weight-bold q-mb-sm">
        Session-Einstellungen
      </div>
      <div class="session-settings-grid items-start">
        <TemplateField v-if="has('template')" />
        <MessageSizeField v-if="has('messageSize')" />
        <AgentField v-if="has('agent')" />
        <div v-if="has('computer')" class="session-settings-grid items-start" style="grid-column: 1 / -1">
          <div><ComputerField /></div>
          <DirectoryField />
        </div>
        <MemoryField v-if="has('memory')" />
        <TrustField v-if="has('trust') && auth.modelTrustEnabled" />
        <WebSearchField v-if="has('webSearch')" />
      </div>
    </section>

    <section v-if="grouped && hasLlmFields" class="session-settings-group" aria-labelledby="session-llm-settings-title">
      <div id="session-llm-settings-title" class="text-subtitle1 text-weight-bold q-mb-sm">
        LLM-Einstellungen
      </div>
      <div class="session-settings-grid items-start">
        <ModelField v-if="has('model')" />
        <ReasoningField v-if="has('reasoning')" />
      </div>
    </section>

    <template v-if="!grouped">
      <TemplateField v-if="has('template')" />
      <MessageSizeField v-if="has('messageSize')" />
      <AgentField v-if="has('agent')" />
      <ModelField v-if="has('model')" />
      <ReasoningField v-if="has('reasoning') && (!compact || reasoningEffortOptions.length)" />
      <ComputerField v-if="has('computer')" />
      <DirectoryField v-if="has('computer') && !compact" />
      <MemoryField v-if="has('memory')" />
      <TrustField v-if="has('trust') && auth.modelTrustEnabled" />
      <WebSearchField v-if="has('webSearch')" />
    </template>
    <ExecutionAgentDirectoryPickerDialog
      v-if="directoryPickerOpen"
      v-model="directoryPickerOpen"
      :execution-agent-id="session?.preferredExecutionAgentId"
      :initial-path="session?.currentDirectory || selectedExecutionAgent?.homeDirectory || '/'"
      @select="update({ current_directory: $event })"
    />
  </div>
</template>

<script setup>
import { computed, defineComponent, h, ref, watch } from 'vue'
import { QBanner, QBtn, QIcon, QInput, useQuasar } from 'quasar'
import AppSelect from 'src/shared/ui/AppSelect.vue'
import ExecutionAgentDirectoryPickerDialog from 'src/features/computers/ExecutionAgentDirectoryPickerDialog.vue'
import { agentDisplayName } from 'src/features/computers/display'
import { useAgentProfileStore } from 'stores/agent-profiles'
import { useExecutionAgentStore } from 'stores/execution-agents'
import { useIntegrationStore } from 'stores/integrations'
import { useProviderStore } from 'stores/providers'
import { apiErrorMessage } from 'src/utils/api-errors'
import { modelReasoningEffortOptions, reasoningEffortLabel } from 'src/utils/model-runtime'
import { sessionModel } from '../session-model'
import { useSessionTemplateStore } from 'src/features/settings/session-templates-store'
import { useAuthStore } from 'src/features/auth/store'
import { useConfirm } from 'src/composables/confirm'

defineOptions({ name: 'SessionSettingsFields' })

const SESSION_FIELDS = ['template', 'messageSize', 'agent', 'computer', 'memory', 'trust', 'webSearch']
const LLM_FIELDS = ['model', 'reasoning']
const props = defineProps({
  session: { type: Object, default: null },
  disabled: Boolean,
  agentProfileId: { type: String, default: null },
  compact: Boolean,
  equalFields: Boolean,
  grouped: Boolean,
  showWarning: Boolean,
  fields: { type: Array, default: () => ['agent', 'model', 'reasoning', 'computer', 'memory', 'trust', 'webSearch', 'messageSize'] }
})
const emit = defineEmits(['update', 'agent-popup-hide'])
const $q = useQuasar()
const agentProfiles = useAgentProfileStore()
const executionAgents = useExecutionAgentStore()
const integrations = useIntegrationStore()
const providers = useProviderStore()
const templates = useSessionTemplateStore()
const auth = useAuthStore()
const { confirm } = useConfirm()

const has = (field) => props.fields.includes(field)
const hasSessionFields = computed(() => SESSION_FIELDS.some(has))
const hasLlmFields = computed(() => LLM_FIELDS.some(has))
const templateId = computed(() => props.session?.sessionProfile?.templateId || props.session?.sessionTemplateId || null)
const templateOptions = computed(() => {
  const options = templates.templates.map((item) => ({ label: `${item.name}${item.scope === 'personal' ? ' (Persönlich)' : ''}`, value: item.id }))
  if (templateId.value && !options.some((item) => item.value === templateId.value)) {
    options.push({ label: 'Nicht verfügbare Vorlage', value: templateId.value, disable: true })
  }
  return options
})
const displayedAgentProfileId = computed(() => (
  props.agentProfileId || props.session?.agentProfileId || agentProfiles.defaultAgentProfileId
))
const agentProfileOptions = computed(() => {
  const options = agentProfiles.agents.filter((agent) => !props.session?.sessionProfile?.agentIds || props.session.sessionProfile.agentIds.includes(agent.id)).map((agent) => ({
    label: agent.name,
    value: agent.id,
    icon: 'smart_toy',
    caption: agent.description
  }))
  if (displayedAgentProfileId.value && !agentProfiles.byId(displayedAgentProfileId.value)) {
    options.unshift({
      label: 'Nicht verfügbares Agent-Profil',
      value: displayedAgentProfileId.value,
      icon: 'warning_amber',
      caption: 'Nicht mehr verfügbar',
      disable: true
    })
  }
  return options
})
const selectedAgentIcon = computed(() => (
  !displayedAgentProfileId.value || agentProfiles.byId(displayedAgentProfileId.value) ? 'smart_toy' : 'warning_amber'
))
const selectedExecutionAgent = computed(() => (
  executionAgents.byId(props.session?.preferredExecutionAgentId)
))
const directoryPickerOpen = ref(false)
const directoryPickerAvailable = computed(() => !props.disabled && selectedExecutionAgent.value?.connected === true
  && !(props.session?.id && props.session?.workspaceMode === 'worktree')
  && selectedExecutionAgent.value.operations?.includes('filesystem.list'))
watch([() => props.session?.id, () => props.session?.preferredExecutionAgentId], () => {
  directoryPickerOpen.value = false
})
const executionAgentOptions = computed(() => {
  const options = [
    { label: 'Kein Computer', value: null, connected: null, icon: 'computer', caption: 'Nur Backend-Funktionen verwenden' },
    ...executionAgents.items.map((agent) => ({
      label: agentDisplayName(agent),
      value: agent.id,
      connected: agent.connected,
      icon: agent.connected === false ? 'cloud_off' : 'computer',
      caption: agent.pairingState === 'pending'
        ? 'Kopplung ausstehend'
        : agent.pairingState === 'unpaired'
          ? 'Nicht gekoppelt'
          : agent.connected ? agent.computerName || agent.platform : 'Offline'
    }))
  ]
  if (props.session?.preferredExecutionAgentId && !selectedExecutionAgent.value) {
    options.push({
      label: 'Nicht verfügbarer Computer',
      value: props.session.preferredExecutionAgentId,
      connected: false,
      icon: 'cloud_off',
      caption: 'Nicht mehr eingerichtet'
    })
  }
  return options
})
const liveWorkspaceContext = computed(() => executionAgents.workspaceContextFor(props.session))
const executionAgentMessage = computed(() => {
  if (executionAgents.loadFailed) return 'Der Status der Computer konnte nicht geladen werden.'
  if (!props.session?.preferredExecutionAgentId) return 'Ohne Computer stehen keine lokalen Datei- und Shell-Funktionen zur Verfügung.'
  if (!selectedExecutionAgent.value) return 'Der ausgewählte Computer ist nicht mehr eingerichtet.'
  if (selectedExecutionAgent.value.pairingState === 'pending') return 'Die Kopplung des ausgewählten Computers steht noch aus.'
  if (selectedExecutionAgent.value.pairingState === 'unpaired') return 'Der ausgewählte Computer ist nicht gekoppelt.'
  if (!selectedExecutionAgent.value.connected) return 'Der ausgewählte Computer ist offline. Es erfolgt kein automatischer Wechsel.'
  if (!selectedExecutionAgent.value.operations?.includes('filesystem.list')) return 'Der ausgewählte Computer bietet keinen Dateizugriff an.'
  if (!liveWorkspaceContext.value.connected) return 'Der ausgewählte Computer ist nicht verfügbar.'
  return ''
})
const effectiveSelection = computed(() => sessionModel(props.session, providers, displayedAgentProfileId.value))
const effectiveModelId = computed(() => effectiveSelection.value.modelId)
const modelSelectionOptions = computed(() => providers.selectionOptions(effectiveModelId.value, true))
const selectedModelLabel = computed(() => {
  const option = modelSelectionOptions.value.find((item) => item.value === effectiveModelId.value)
  return option?.primaryLabel || option?.label || 'Modell wählen'
})
const selectedModelConfig = computed(() => providers.models.find((model) => model.id === effectiveModelId.value))
const selectedModelTrustIcon = computed(() => selectedModelConfig.value?.effectiveTrustLevel === 'private' ? 'lock' : 'public')
const sessionTrust = computed(() => props.session?.trustLevel || selectedModelConfig.value?.effectiveTrustLevel || 'private')
const reasoningEffortOptions = computed(() => modelReasoningEffortOptions(selectedModelConfig.value))
const modelSupportsTools = computed(() => selectedModelConfig.value?.supportsTools !== false)
const memoryModeOptions = [
  { label: 'Vollständig', value: 'full', icon: 'memory', description: 'Vorherige Chats und Erinnerungen nutzen; neue Erinnerungen speichern' },
  { label: 'Lesen', value: 'read_only', icon: 'visibility', description: 'Vorherige Chats und Erinnerungen nutzen; nichts speichern' },
  { label: 'Deaktiviert', value: 'disabled', icon: 'memory', description: 'Keine Inhalte aus vorherigen Chats oder Erinnerungen nutzen' }
]
const effectiveMemoryMode = computed(() => props.session?.effectiveMemoryMode || props.session?.memoryMode || 'disabled')
const memoryMessage = computed(() => {
  if (props.session?.memoryUnavailableReason) {
    return apiErrorMessage({ error: { code: props.session.memoryUnavailableReason } })
  }
  return {
    full: 'Vorherige Chats und Erinnerungen nutzen; neue Erinnerungen speichern.',
    read_only: 'Vorherige Chats und Erinnerungen nutzen; keine neuen Erinnerungen speichern.',
    disabled: 'Keine Inhalte aus vorherigen Chats oder Erinnerungen nutzen.'
  }[effectiveMemoryMode.value]
})
const memoryModeIcon = computed(() => ({ full: 'memory', read_only: 'visibility', disabled: 'memory' })[props.session?.memoryMode] || 'memory')
const searchAvailable = computed(() => integrations.search.effectiveEnabled)
const webSearchOptions = computed(() => [
  {
    label: searchAvailable.value && modelSupportsTools.value ? 'Erzwingen' : 'Erzwingen (nicht verfügbar)',
    shortLabel: searchAvailable.value && modelSupportsTools.value ? 'Pflicht' : 'Nicht verfügbar',
    value: 'required',
    icon: 'travel_explore',
    disable: !searchAvailable.value || !modelSupportsTools.value,
    description: 'Vor jeder Antwort erfolgreich im Web suchen'
  },
  {
    label: searchAvailable.value && modelSupportsTools.value ? 'Verfügbar' : 'Nicht verfügbar',
    shortLabel: searchAvailable.value && modelSupportsTools.value ? 'Auto' : 'Nicht verfügbar',
    value: 'available',
    icon: 'public',
    description: 'Das Modell entscheidet über die Websuche'
  },
  { label: 'Deaktiviert', shortLabel: 'Aus', value: 'disabled', icon: 'public_off', description: 'Kein Websuch-Tool anbieten' }
])
const webSearchIcon = computed(() => ({
  required: 'travel_explore',
  available: 'public',
  disabled: 'public_off'
})[props.session?.webSearchPolicy] || 'public')
const webSearchTooltip = computed(() => {
  if (!modelSupportsTools.value) return 'Das ausgewählte Modell unterstützt keine Tools.'
  if (!searchAvailable.value) {
    return integrations.search.unavailableReason === 'integration.disabled'
      ? 'Websuche ist deaktiviert'
      : 'Websuche ist nicht eingerichtet'
  }
  return ''
})
const webSearchMessage = computed(() => (
  webSearchTooltip.value
  || webSearchOptions.value.find((option) => option.value === props.session?.webSearchPolicy)?.description
  || 'Websuche für diese Session konfigurieren.'
))
const requiredSearchUnavailable = computed(() => (
  props.session?.webSearchPolicy === 'required' && (!searchAvailable.value || !modelSupportsTools.value)
))
const computerInfo = computed(() => (
  has('computer') && !executionAgents.loadFailed && !props.session?.preferredExecutionAgentId
    ? executionAgentMessage.value : ''
))
const warning = computed(() => {
  const warnings = []
  const profile = agentProfiles.byId(displayedAgentProfileId.value)
  if (['00000000-0000-4000-8000-000000000003', '00000000-0000-4000-8000-000000000004'].includes(profile?.baseProfileId || displayedAgentProfileId.value)
    && !props.session?.preferredExecutionAgentId) warnings.push('Plan und Build benötigen einen Computer und ein Arbeitsverzeichnis.')
  if (!modelSupportsTools.value) warnings.push('Wähle ein Agentmodell mit Tool-Unterstützung.')
  if (
    props.session?.memoryMode !== 'disabled'
    && props.session?.memoryUnavailableReason !== 'session.modelUnavailable'
    && (props.session?.memoryUnavailableReason || (!props.session?.memoryToolsAvailable && effectiveMemoryMode.value !== 'disabled'))
  ) warnings.push(memoryMessage.value)
  if (requiredSearchUnavailable.value) warnings.push(webSearchTooltip.value || 'Die erforderliche Websuche ist nicht verfügbar.')
  if (has('computer') && executionAgentMessage.value && !computerInfo.value) warnings.push(executionAgentMessage.value)
  return warnings.join(' ')
})

function update(patch) {
  if (props.session && !props.disabled) {
    emit('update', patch)
  }
}

async function selectTrust(value) {
  if (value === sessionTrust.value) return
  if (props.session?.id && sessionTrust.value === 'private' && value === 'public'
    && !await confirm('Die gesamte bisherige Session kann danach auch von öffentlichen Modellen in anderen Sessions genutzt werden. Bereits erzeugte Erinnerungen bleiben unverändert.', 'Session als öffentlich einstufen?', { okLabel: 'Umstellen' })) return
  update({ trust_level: value })
}

function selectModel(modelId) {
  const model = providers.models.find((item) => item.id === modelId)
  const agentId = displayedAgentProfileId.value
  const defaultEffort = props.session?.reasoningEffort || providers.modelDefaults?.profileModels?.[agentId]?.reasoningEffort || providers.modelDefaults?.reasoningEffort
  update({ session_profile: (profile) => {
    const effort = profile?.agentModels?.[agentId]?.reasoningEffort || defaultEffort
    const agentModels = { ...profile?.agentModels }
    agentModels[agentId] = { ...agentModels[agentId], modelId, ...(effort && !model?.reasoningEfforts?.includes(effort) ? { reasoningEffort: null } : {}) }
    return { ...profile, agentModels }
  } })
}

function selectReasoning(reasoningEffort) {
  const agentId = displayedAgentProfileId.value
  update({ session_profile: (profile) => ({ ...profile, agentModels: {
    ...profile?.agentModels, [agentId]: { ...profile?.agentModels?.[agentId], reasoningEffort }
  } }) })
}

function selectExecutionAgent(value) {
  const agent = executionAgents.byId(value)
  update({
    preferred_execution_agent_id: value,
    current_directory: agent?.homeDirectory || null
  })
}

const selectProps = (extra = {}) => ({
  outlined: !props.compact,
  borderless: props.compact,
  dense: true,
  emitValue: true,
  mapOptions: true,
  menuShrink: true,
  menuAnchor: props.compact ? 'top start' : 'bottom start',
  menuSelf: props.compact ? 'bottom start' : 'top start',
  disable: props.disabled,
  ...extra
})
const TemplateField = defineComponent(() => () => h(AppSelect, selectProps({
  modelValue: templateId.value,
  options: templateOptions.value,
  label: 'Sessionvorlage',
  disable: props.disabled || templates.loading,
  hint: 'Ein Wechsel übernimmt Agenten, Memory, Nachrichtengröße sowie Modell- und Reasoningvorgaben neu.',
  persistentHint: true,
  'onUpdate:modelValue': (value) => update({ session_template_id: value })
}), { prepend: () => h(QIcon, { name: 'dashboard_customize' }) }))
const MessageSizeField = defineComponent(() => () => h(AppSelect, selectProps({
  modelValue: props.session?.sessionProfile?.messageSize || 'standard',
  options: [{ label: 'Standard', value: 'standard' }, { label: 'Klein', value: 'small' }],
  label: 'Nachrichtengröße',
  hint: 'Klein reduziert die Schriftgröße und die Abstände der Nachrichten sowie die Schriftgröße im Eingabefeld am Desktop.',
  persistentHint: true,
  'onUpdate:modelValue': (messageSize) => update({ session_profile: (profile) => ({ ...profile, messageSize }) })
}), { prepend: () => h(QIcon, { name: 'format_size' }) }))
const AgentField = defineComponent(() => () => h(AppSelect, selectProps({
  modelValue: displayedAgentProfileId.value,
  options: agentProfileOptions.value,
  label: props.compact ? undefined : 'Agent-Profil',
  'aria-label': 'Agent-Profil auswählen',
  dropdownIcon: props.compact ? 'expand_more' : undefined,
  hideBottomSpace: props.compact,
  displayValue: props.compact ? agentProfileOptions.value.find((option) => option.value === displayedAgentProfileId.value)?.label || 'Agent wählen' : undefined,
  class: props.compact ? 'composer-select composer-agent-mode col-auto rounded-borders q-px-sm' : undefined,
  hint: props.compact ? undefined : 'Agent-Profil für diese Session.',
  persistentHint: !props.compact,
  disable: props.disabled || agentProfiles.loading,
  onPopupHide: () => emit('agent-popup-hide'),
  'onUpdate:modelValue': (value) => update({ agent_profile_id: value })
}), {
  prepend: !props.compact || selectedAgentIcon.value === 'warning_amber'
    ? () => h(QIcon, { name: selectedAgentIcon.value, size: props.compact ? '16px' : undefined })
    : undefined
}))
const ModelField = defineComponent(() => () => h(AppSelect, selectProps({
  modelValue: effectiveModelId.value,
  options: props.compact ? [{ label: 'Standardmodell verwenden', value: null }, ...modelSelectionOptions.value] : modelSelectionOptions.value,
  label: props.compact ? undefined : 'Modell',
  'aria-label': 'Modell auswählen',
  dropdownIcon: props.compact ? 'expand_more' : undefined,
  hideBottomSpace: props.compact,
  displayValue: props.compact || !effectiveModelId.value ? selectedModelLabel.value : undefined,
  class: props.compact ? 'composer-select composer-model col-auto rounded-borders q-px-sm' : undefined,
  hint: props.compact ? undefined : modelSupportsTools.value ? 'Modell für diesen Agenten in dieser Session.' : 'Das ausgewählte Modell unterstützt keine Tools.',
  persistentHint: !props.compact,
  clearable: !props.compact,
  'onUpdate:modelValue': selectModel
}), {
  prepend: () => h(QIcon, {
    name: selectedModelTrustIcon.value,
    size: props.compact ? '16px' : undefined,
    role: 'img',
    'aria-hidden': 'false',
    'aria-label': `Datenfreigabe: ${selectedModelTrustIcon.value === 'lock' ? 'Privat' : 'Öffentlich'}`
  })
}))
const ReasoningField = defineComponent(() => () => h(AppSelect, selectProps({
  modelValue: effectiveSelection.value.reasoningEffort,
  options: reasoningEffortOptions.value,
  label: props.compact ? undefined : 'Denken',
  'aria-label': 'Reasoning-Aufwand auswählen',
  dropdownIcon: props.compact ? 'expand_more' : undefined,
  hideBottomSpace: props.compact,
  displayValue: props.compact ? effectiveSelection.value.reasoningEffort ? reasoningEffortLabel(effectiveSelection.value.reasoningEffort) : 'Standard' : undefined,
  class: props.compact ? 'composer-select composer-reasoning col-auto rounded-borders q-px-sm' : undefined,
  hint: props.compact ? undefined : reasoningEffortOptions.value.length
    ? 'Reasoning-Aufwand für diesen Agenten in dieser Session.'
    : 'Das ausgewählte Modell bietet keine Reasoning-Stufen an.',
  persistentHint: !props.compact,
  disable: props.disabled || !reasoningEffortOptions.value.length,
  'onUpdate:modelValue': selectReasoning
})))
const ComputerField = defineComponent(() => () => [
  h(AppSelect, selectProps({
    disable: props.disabled || Boolean(props.session?.id && props.session?.workspaceMode === 'worktree'),
    modelValue: props.session?.preferredExecutionAgentId,
    options: executionAgentOptions.value,
    label: props.compact ? undefined : 'Computer',
    class: props.compact ? 'chat-start-select col-auto q-px-sm text-body2' : undefined,
    hideBottomSpace: props.compact,
    'aria-label': 'Computer auswählen',
    hint: props.compact ? undefined : 'Der Computer wird für den nächsten Lauf verwendet.',
    persistentHint: !props.compact,
    'onUpdate:modelValue': selectExecutionAgent
  }), {
    prepend: () => h(QIcon, {
      name: props.compact && selectedExecutionAgent.value?.connected === false ? 'cloud_off' : 'computer',
      size: props.compact ? '18px' : undefined,
      color: props.compact && props.session?.preferredExecutionAgentId && executionAgentMessage.value ? 'warning' : undefined
    }),
    selected: props.compact ? () => h('span', { class: 'ellipsis' }, executionAgentOptions.value.find(
      (option) => option.value === (props.session?.preferredExecutionAgentId || null)
    )?.label) : undefined
  }),
  executionAgentMessage.value && !props.showWarning && !props.compact
    ? h(QBanner, { dense: true, rounded: true, class: 'app-surface-tonal', role: warning.value ? 'alert' : 'status' }, {
        avatar: () => h(QIcon, {
          name: selectedExecutionAgent.value?.connected === false ? 'cloud_off' : 'info_outline',
          color: warning.value ? 'warning' : 'info'
        }),
        default: () => executionAgentMessage.value
      })
    : null
])
const DirectoryField = defineComponent(() => () => h(QInput, {
  modelValue: props.session?.currentDirectory || '',
  outlined: true, dense: true, readonly: true, label: 'Arbeitsverzeichnis', class: 'session-directory',
  disable: props.disabled || !selectedExecutionAgent.value?.connected
}, {
  append: () => h(QBtn, {
    flat: true, round: true, dense: true, icon: 'folder_open', 'aria-label': 'Arbeitsverzeichnis auswählen',
    disable: !directoryPickerAvailable.value, onClick: () => { directoryPickerOpen.value = true }
  })
}))
const MemoryField = defineComponent(() => () => h(AppSelect, selectProps({
  modelValue: props.session?.memoryMode,
  options: memoryModeOptions,
  label: 'Memory',
  stackLabel: props.compact,
  class: props.compact ? 'chat-start-select col-auto q-px-sm text-body2' : undefined,
  hideBottomSpace: props.compact,
  'aria-label': 'Memory-Modus auswählen',
  hint: props.compact ? undefined : memoryMessage.value,
  persistentHint: !props.compact,
  'onUpdate:modelValue': (value) => update({ memory_mode: value })
}), {
  prepend: () => h(QIcon, {
    name: memoryModeIcon.value,
    size: props.compact ? '18px' : undefined,
    color: props.compact && props.session?.memoryMode !== 'disabled' && props.session?.memoryUnavailableReason ? 'warning' : undefined
  }),
  selected: props.compact ? () => h('span', { class: 'ellipsis' }, memoryModeOptions.find(
    (option) => option.value === props.session?.memoryMode
  )?.label || 'Nicht verfügbar') : undefined
}))
const TrustField = defineComponent(() => () => h(AppSelect, selectProps({
  modelValue: sessionTrust.value,
  options: [{ label: 'Privat', value: 'private' }, { label: 'Öffentlich', value: 'public' }],
  label: 'Vertraulichkeit',
  hint: 'Gilt für den gesamten Verlauf und neue Erinnerungen. Die Modellwahl bleibt unabhängig.',
  'onUpdate:modelValue': selectTrust
})))
const WebSearchField = defineComponent(() => () => h(AppSelect, selectProps({
  modelValue: props.session?.webSearchPolicy,
  options: webSearchOptions.value,
  label: props.compact ? undefined : 'Websuche',
  class: props.compact ? 'chat-start-select col-auto q-px-sm text-body2' : undefined,
  hideBottomSpace: props.compact,
  'aria-label': 'Websuche konfigurieren',
  hint: props.compact ? undefined : webSearchMessage.value,
  persistentHint: !props.compact,
  'onUpdate:modelValue': (value) => update({ web_search_policy: value })
}), {
  prepend: () => h(QIcon, { name: webSearchIcon.value, size: props.compact ? '18px' : undefined, color: props.compact && requiredSearchUnavailable.value ? 'warning' : undefined }),
  selected: props.compact ? () => h('span', { class: 'ellipsis' }, `${$q.screen.lt.sm ? 'Web' : 'Websuche'}: ${webSearchOptions.value.find(
    (option) => option.value === props.session?.webSearchPolicy
  )?.[$q.screen.lt.sm ? 'shortLabel' : 'label'] || 'Nicht verfügbar'}`) : undefined
}))

defineExpose({ warning })
</script>
