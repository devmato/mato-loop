<template>
  <template v-if="!history">
    <SubagentRows :children="activeSubagents" class="q-mb-xs" />
    <slot />
  </template>
  <div
    v-else-if="$slots.default || entries.length || running || unmatchedSubagents.length"
    class="work-history"
  >
    <div class="work-history__meta-section float-right row items-center no-wrap">
      <div v-if="historyExpanded" class="work-history__meta row items-center no-wrap q-gutter-xs">
        <TurnDuration :history="running ? activity : history" :running="running" />
      </div>
      <q-btn
        flat round dense
        size="sm"
        class="work-history__toggle"
        :class="{ 'q-ml-sm': historyExpanded }"
        icon="history"
        :color="historyExpanded ? 'primary' : undefined"
        :aria-expanded="historyExpanded"
        :aria-label="historyExpanded ? 'Verlauf ausblenden' : 'Verlauf anzeigen'"
        @click="toggleHistory"
      >
        <q-tooltip>{{ historyExpanded ? 'Verlauf ausblenden' : 'Verlauf anzeigen' }}</q-tooltip>
      </q-btn>
    </div>

    <div v-if="displayEntries.length || activeSubagents.length || $slots.default" class="work-history__timeline">
      <SubagentRows v-if="!historyExpanded" :children="activeSubagents" class="q-mb-xs" />
      <div v-if="displayEntries.length" class="work-history__entries" role="region" aria-label="Aktivitäten der Antworterstellung">
        <AgentActivityEntry
          v-for="entry in displayEntries"
          :key="entry.key"
          :entry="entry"
          :expanded-details="expandedDetails"
          :trusted-image-origins="trustedImageOrigins"
          :read-only="readOnly"
          :show-output="historyExpanded"
          show-supplementary
          @open-file="emit('open-file', $event)"
          @rendered="emit('rendered')"
          @resize-start="emit('resize-start')"
          @resized="emit('resized')"
          @toggle-details="toggleDetails"
          @trust-image-origin="emit('trust-image-origin', $event)"
        />
      </div>
      <slot />
    </div>
    <div v-if="!historyExpanded && outputEntries.length" class="work-history__persistent-outputs">
      <AgentActivityEntry
        v-for="entry in outputEntries"
        :key="entry.key"
        :entry="entry"
        :expanded-details="expandedDetails"
        :read-only="readOnly"
        output-only
        show-output
        @open-file="emit('open-file', $event)"
      />
    </div>
    <SubagentRows v-if="historyExpanded" class="work-history__children" :children="unmatchedSubagents" />
  </div>
</template>

<script setup>
import { computed, nextTick, ref } from 'vue'
import AgentActivityEntry from 'src/features/chat/components/AgentActivityEntry.vue'
import SubagentRows from 'src/features/subagents/SubagentRows.vue'
import { isActiveSubagent } from 'stores/subagents'
import TurnDuration from 'src/features/chat/components/TurnDuration.vue'
import {
  toolOutputAttachments,
  toolFileActions,
  toolFileAttachment
} from 'src/features/files/attachments'
import { agentRunPresentation, hiddenToolRepair, runWarningEntries } from 'src/features/chat/agent-activity'
import { groupWorkHistoryEntries } from 'src/features/chat/transcript'
import { reasoningBlocks, reasoningHistoryPresentation } from 'src/features/chat/reasoning-display'
import { useInstanceLimitsStore } from 'stores/instance-limits'

defineOptions({ name: 'AgentActivityPanel' })

const props = defineProps({
  history: { type: Object, default: null },
  activity: { type: Object, default: null },
  presentation: { type: Object, default: null },
  reasoning: { type: Object, default: null },
  contextSummary: { type: Object, default: null },
  warnings: { type: Array, default: () => [] },
  subagents: { type: Array, default: () => [] },
  showActiveSubagents: { type: Boolean, default: true },
  trustedImageOrigins: { type: Array, default: () => [] },
  readOnly: Boolean,
  hideProvidedFiles: Boolean,
  running: Boolean,
  expanded: { type: Boolean, default: null }
})
const emit = defineEmits(['history-expanded', 'open-file', 'rendered', 'resize-start', 'resized', 'trust-image-origin'])
const instanceLimits = useInstanceLimitsStore()
const localHistoryExpanded = ref(false)
const historyExpanded = computed(() => props.expanded ?? localHistoryExpanded.value)
const expandedDetails = ref(new Set())
function toggleDetails(key) {
  if (expandedDetails.value.has(key)) expandedDetails.value.delete(key)
  else expandedDetails.value.add(key)
}
function toggleHistory() {
  emit('resize-start')
  const expanded = !historyExpanded.value
  if (props.expanded === null) localHistoryExpanded.value = expanded
  emit('history-expanded', expanded)
  nextTick(() => emit('resized'))
}
const presentation = computed(() => props.presentation || agentRunPresentation({
  running: props.running, activity: props.activity,
  streamReasoning: props.reasoning, streamContextSummary: props.contextSummary,
  transcript: { messages: [{ workHistory: props.history }] }
}))
const currentBatch = computed(() => props.running && props.activity?.phase === 'processing_tool'
  && presentation.value.entry?.type === 'tool' && !presentation.value.showingScriptChildFailure
  ? presentation.value.entry.batchId : null)
const preparedEntries = computed(() => {
  const current = presentation.value.entry
  const history = (props.history?.entries || []).filter((entry) => !hiddenToolRepair(entry, props.running) && !(entry.type === 'reasoning' && entry.live) && (
    entry.type !== 'tool' || !props.running || entry.result || entry.completed || entry.approval || entry.question
      || entry.key === current?.key || (!historyExpanded.value && currentBatch.value != null && entry.batchId === currentBatch.value)
  )).map((entry) => {
    const prepared = entry.key === current?.key ? current : { ...entry, active: false }
    return prepared.type === 'reasoning' ? {
      ...prepared,
      blocks: reasoningBlocks(prepared.reasoning),
      presentation: reasoningHistoryPresentation(prepared.reasoning)
    } : prepared
  })
  return [
    ...history,
    ...runWarningEntries(props.warnings, 'live-warning', true),
    ...(current && !history.some((entry) => entry.key === current.key) ? [current] : [])
  ]
})
const enrichedEntries = computed(() => preparedEntries.value.map((entry) => {
  if (entry.type !== 'tool') return entry
  const providedFile = toolFileAttachment(
    entry.tool,
    entry.result,
    instanceLimits.maxImagePreviewBytes
  )
  return {
    ...entry,
    subagents: props.subagents.filter((child) => (
      child.spawnedByToolCallId === entry.tool?.id && child.spawnedByRunId === entry.runId
    )),
    output: {
      attachments: toolOutputAttachments(entry.tool, [
        ...(entry.result?.attachments || []),
        ...(providedFile ? [providedFile] : [])
      ]),
      fileActions: props.hideProvidedFiles ? [] : toolFileActions(entry.result).filter((action) => action.operation === 'provide')
    }
  }
}))
const outputEntries = computed(() => enrichedEntries.value.filter((entry) => (
  entry.type === 'tool' && (entry.output.attachments.length || entry.output.fileActions.length)
)))
const entries = computed(() => groupWorkHistoryEntries(
  enrichedEntries.value, enrichedEntries.value.find((entry) => entry.key === presentation.value.entry?.key)
))
const currentEntries = computed(() => {
  const current = presentation.value.entry
  if (!current) return []
  const latestCommentary = current.tool?.id || props.activity?.phase === 'waiting_model'
    ? enrichedEntries.value.findLast((entry) => entry.type === 'commentary' && entry.toolCallIds?.length)
    : null
  return enrichedEntries.value.filter((entry) => entry.key === current.key
    || (currentBatch.value != null && entry.type === 'tool' && entry.batchId === currentBatch.value) || (
    entry.type === 'commentary' && (entry === latestCommentary || (
      current.tool?.id && entry.toolCallIds?.includes(current.tool.id)
    ))
  ))
})
const displayEntries = computed(() => {
  if (historyExpanded.value) return entries.value
  const current = currentEntries.value.filter((entry) => !entry.subagents?.length || (props.showActiveSubagents && entry.subagents.some(isActiveSubagent))).map((entry) => ({
    ...entry, subagents: entry.subagents?.filter(isActiveSubagent)
  }))
  return currentBatch.value != null ? groupWorkHistoryEntries(current, null, true) : current
})
const activeSubagents = computed(() => {
  if (!props.showActiveSubagents) return []
  const visible = new Set(currentEntries.value.flatMap((entry) => (entry.subagents || []).map((child) => child.id)))
  return props.subagents.filter((child) => isActiveSubagent(child) && !visible.has(child.id))
})
const items = computed(() => enrichedEntries.value.filter((entry) => entry.type === 'tool'))
const unmatchedSubagents = computed(() => {
  const matched = new Set(items.value.flatMap((item) => item.subagents || []).map((child) => child.id))
  return props.subagents.filter((child) => !matched.has(child.id))
})
</script>
