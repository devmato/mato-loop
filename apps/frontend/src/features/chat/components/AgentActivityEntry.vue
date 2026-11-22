<template>
  <SubagentRows v-if="entry.type === 'tool' && showSupplementary && entry.subagents?.length" :children="entry.subagents" />
  <div v-else-if="entry.type === 'tool-group'" class="work-history__tool-group">
    <q-item
      dense clickable manual-focus
      class="activity-row work-history__tool-group-header"
      :aria-expanded="detailsOpen"
      :aria-controls="detailsId"
      :aria-label="detailsLabel"
      @click="toggleDetails"
    >
      <q-item-section>
        <q-item-label class="work-history__activity-line">
          <q-icon
            :name="identity.icon"
            size="18px"
            class="work-history__activity-icon"
            :class="{ 'work-history__activity-title--past': !groupActive }"
            aria-hidden="true"
          />
          <span
            class="work-history__activity-title"
            :class="{
              'activity-shimmer': groupActive,
              'work-history__activity-title--past': !groupActive
            }"
          >
            {{ identity.label }}
          </span>
          <span
            class="work-history__activity-summary"
            :aria-label="`${entry.items.length} Aufrufe`"
          >· {{ entry.items.length }}</span>
          <q-icon
            :name="detailsOpen ? 'expand_more' : 'chevron_right'"
            size="17px"
            class="work-history__detail-indicator"
            aria-hidden="true"
          />
        </q-item-label>
      </q-item-section>
      <q-item-section v-if="statusIcon" side class="tool-call__state">
        <q-icon :name="statusIcon" size="17px" :class="`tool-call__status--${status.name}`" role="img" :aria-label="status.label" />
      </q-item-section>
    </q-item>
    <div :id="detailsId" class="work-history__tool-group-items">
      <AgentActivityEntry
        v-for="item in entry.items"
        :key="item.key"
        :entry="{ ...item, detailOnly: true }"
        :output-only="!detailsOpen"
        :expanded-details="expandedDetails"
        :trusted-image-origins="trustedImageOrigins"
        :read-only="readOnly"
        :show-output="showOutput"
        :show-supplementary="showSupplementary"
        @open-file="emit('open-file', $event)"
        @rendered="emit('rendered')"
        @resize-start="emit('resize-start')"
        @resized="emit('resized')"
        @toggle-details="emit('toggle-details', $event)"
        @trust-image-origin="emit('trust-image-origin', $event)"
      />
    </div>
  </div>
  <div
    v-else-if="!outputOnly && ['tool', 'reasoning', 'context-summary'].includes(entry.type)"
    :class="isTool ? 'work-history__tool' : hasDetails ? 'work-history__reasoning' : 'work-history__reasoning-static'"
  >
    <AgentActivityRow
      v-if="!entry.detailOnly || summary || hasDetails || statusIcon"
      manual-focus
      :class="isTool ? ['tool-call tool-call__header', `tool-call--${status.name}`, { 'tool-call--detail': entry.detailOnly }] : 'work-history__reasoning-header'"
      :clickable="hasDetails"
      :aria-expanded="hasDetails ? detailsOpen : undefined"
      :aria-controls="hasDetails ? detailsId : undefined"
      :aria-label="hasDetails ? detailsLabel : undefined"
      @click="toggleDetails"
    >
      <q-icon
        v-if="identity.icon && !entry.detailOnly"
        :name="identity.icon"
        size="18px"
        class="work-history__activity-icon"
        :class="{ 'work-history__activity-title--past': !entry.active }"
        aria-hidden="true"
      />
      <span
        v-if="!entry.detailOnly || !summary"
        class="work-history__activity-title"
        :class="{ 'tool-call__title': isTool, 'activity-shimmer': entry.active, 'work-history__activity-title--past': !entry.active }"
      >{{ identity.label }}<span v-if="entry.presentation?.duration" class="work-history__reasoning-duration text-no-wrap"> · {{ entry.presentation.duration }}</span></span>
      <span v-if="summary" class="work-history__activity-summary" :class="{ 'tool-call__summary': isTool }">{{ summary }}</span>
      <q-icon
        v-if="hasDetails"
        :name="detailsOpen ? 'expand_more' : 'chevron_right'"
        size="17px"
        class="work-history__detail-indicator"
        aria-hidden="true"
      />
      <template #state>
        <q-item-section v-if="statusIcon" side class="tool-call__state">
          <div class="tool-call__status row items-center no-wrap q-gutter-xs" :class="`tool-call__status--${status.name}`">
            <q-icon v-if="statusIcon" :name="statusIcon" size="17px" role="img" :aria-label="status.label" />
          </div>
        </q-item-section>
      </template>
    </AgentActivityRow>
    <div v-if="hasDetails && detailsOpen" :id="detailsId" class="work-history__reasoning-detail">
      <p v-if="errorDetails" class="tool-call__message" :class="`tool-call__status--${status.name}`" role="status">{{ errorDetails }}</p>
      <template v-if="isTool">
        <p v-if="resultLoading" role="status">Vollständige Tool-Details werden geladen …</p>
        <div v-else-if="resultLoadError" role="status" class="q-mb-sm text-negative">
          {{ resultLoadError }}
          <q-btn flat dense label="Erneut laden" @click="loadFullResult" />
        </div>
        <p v-else-if="tool.argumentsTruncated" role="status">Tool-Argumente sind noch nicht vollständig geladen.</p>
        <ShellToolOutput v-if="shellCommandText" :command="shellCommandText" :output="typeof result.stdout === 'string' ? result : entry.processOutput || {}" />
        <div v-for="(item, index) in toolDetails" :key="index" class="q-mb-sm">
          <div class="text-weight-medium">{{ item.label }}</div>
          <ul v-if="item.format === 'list'" class="q-my-none q-pl-md"><li v-for="(value, i) in item.value" :key="i">{{ value }}</li></ul>
          <pre v-else-if="item.format === 'code'" class="activity-row__code q-ma-none">{{ item.value }}</pre>
          <div v-else>{{ item.value }}</div>
        </div>
        <div v-if="resultMessage?.content && !shellCommandText && !errorDetails" class="q-mb-sm">
          <div class="text-weight-medium">Tool-Ergebnis{{ resultMessage.contentTruncated ? ' (Vorschau)' : '' }}</div>
          <pre class="activity-row__code q-ma-none">{{ resultMessage.content }}</pre>
        </div>
      </template>
      <p v-else-if="entry.type === 'context-summary'" class="work-history__reasoning-block" :class="{ 'text-warning': entry.warning }">{{ entry.text }}</p>
      <template v-else>
        <p v-for="(block, blockIndex) in entry.blocks" :key="blockIndex" class="work-history__reasoning-block">
          <template v-for="(segment, index) in block" :key="index">
            <strong v-if="segment.strong">{{ segment.text }}</strong><span v-else>{{ segment.text }}</span>
          </template>
        </p>
      </template>
    </div>
  </div>
  <q-item v-else-if="entry.type === 'warning'" dense class="text-warning" role="status">
    <q-item-section>
      <q-item-label class="text-weight-medium">{{ entry.presentation.title }}</q-item-label>
      <q-item-label caption class="text-warning">{{ entry.presentation.summary }}</q-item-label>
    </q-item-section>
    <q-item-section side>
      <q-icon name="warning" color="warning" size="17px" aria-label="Warnung" />
    </q-item-section>
  </q-item>
  <div v-else-if="entry.type === 'commentary'" class="q-pt-sm" :class="{ 'activity-shimmer': entry.active }">
    <MessageContent
      :content="entry.message.content"
      :research-sources="entry.message.research?.linkedSources || []"
      :streaming="entry.message.streaming"
      :trusted-image-origins="trustedImageOrigins"
      @rendered="emit('rendered')"
      @trust-image-origin="emit('trust-image-origin', $event)"
    />
    <ChatFiles
      v-if="entry.message.attachments?.length"
      class="q-mt-sm"
      :attachments="entry.message.attachments"
      @open-file="emit('open-file', $event)"
    />
  </div>
  <div v-if="entry.type === 'tool'">
    <div
      v-if="showOutput && (entry.output?.attachments.length || entry.output?.fileActions.length)"
      class="work-history__outputs"
    >
      <div v-if="entry.tool.name !== 'provide_file'" class="tool-call__section-label">Ausgaben</div>
      <ChatFiles
        :attachments="entry.output.attachments"
        :actions="entry.output.fileActions"
        :read-only="readOnly"
        @open-file="emit('open-file', $event)"
      />
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, ref, useId, watch } from 'vue'
import { api } from 'src/boot/axios'
import { useChatStore } from 'src/features/chat/store'
import { useSubagentStore } from 'src/stores/subagents'
import { apiErrorMessage } from 'src/utils/api-errors'
import AgentActivityRow from 'src/features/chat/components/AgentActivityRow.vue'
import ChatFiles from 'src/features/chat/components/ChatFiles.vue'
import MessageContent from 'src/features/chat/components/MessageContent.vue'
import ShellToolOutput from 'src/features/chat/components/ShellToolOutput.vue'
import SubagentRows from 'src/features/subagents/SubagentRows.vue'
import { parseToolResult, shellCommand, TOOL_SCRIPT, toolIdentity, toolPresentation, toolStatus } from 'src/features/chat/tool-display'

defineOptions({ name: 'AgentActivityEntry' })

const props = defineProps({
  entry: { type: Object, required: true },
  expandedDetails: { type: Set, required: true },
  trustedImageOrigins: { type: Array, default: () => [] },
  readOnly: Boolean,
  outputOnly: Boolean,
  showOutput: Boolean,
  showSupplementary: Boolean
})
const emit = defineEmits(['open-file', 'rendered', 'resize-start', 'resized', 'trust-image-origin', 'toggle-details'])
const detailsId = useId()
const isTool = computed(() => props.entry.type === 'tool')
const isGroup = computed(() => props.entry.type === 'tool-group')
const isReasoning = computed(() => props.entry.type === 'reasoning')
const reasoningSummary = computed(() => isReasoning.value
  && ['Nachdenken', 'Nachdenken unterbrochen'].includes(props.entry.presentation?.title)
  ? props.entry.presentation?.summary?.trim() || '' : '')
const identity = computed(() => isTool.value || isGroup.value
  ? toolIdentity(props.entry.tool)
  : {
      label: reasoningSummary.value
        ? `${reasoningSummary.value}${props.entry.reasoning?.status === 'interrupted' ? ' (unterbrochen)' : ''}`
        : props.entry.presentation?.title,
      icon: props.entry.presentation?.icon
    })
const hydratedResult = ref(null)
const hydratedCarrier = ref(null)
watch(() => props.entry.tool, () => { hydratedCarrier.value = null }, { flush: 'sync' })
watch(() => props.entry.result, () => { hydratedResult.value = null }, { flush: 'sync' })
const tool = computed(() => hydratedCarrier.value?.id === props.entry.batchId
  ? hydratedCarrier.value?.toolCalls?.find((call) => call.id === props.entry.tool?.id) || props.entry.tool
  : props.entry.tool)
const resultLoading = ref(false)
const resultLoadError = ref('')
const resultMessage = computed(() => hydratedResult.value && hydratedResult.value.id === props.entry.result?.id
  ? hydratedResult.value : props.entry.result)
const result = computed(() => isTool.value ? parseToolResult(resultMessage.value?.content, props.entry.tool.name, resultMessage.value) : {})
const toolInfo = computed(() => isTool.value ? toolPresentation(tool.value, props.entry.approval?.details?.displayItems) : null)
const summary = computed(() => isTool.value
  ? (tool.value.name === TOOL_SCRIPT ? result.value.errorMessage : result.value.summary) || toolInfo.value.summary
  : reasoningSummary.value ? '' : props.entry.presentation?.summary)
const shellCommandText = computed(() => isTool.value ? shellCommand(tool.value) : '')
const status = computed(() => {
  if (isGroup.value) {
    const statuses = props.entry.items.map(toolStatus)
    return statuses.find((item) => ['failed', 'denied'].includes(item.name))
      || statuses.find((item) => item.name === 'interrupted')
      || statuses.find((item) => ['approval', 'question'].includes(item.name))
      || { name: '', label: '' }
  }
  return isTool.value ? toolStatus({ ...props.entry, result: resultMessage.value }) : { name: props.entry.warning ? 'interrupted' : '', label: 'Warnung' }
})
const statusIcon = computed(() => ({ failed: 'error', denied: 'error', interrupted: 'warning', approval: 'pending_actions', question: 'help_outline' })[status.value.name])
const errorDetails = computed(() => result.value.errorMessage || '')
const toolDetails = computed(() => (toolInfo.value?.items || []).filter((item) => item.value.length && item.value !== summary.value && item.value !== shellCommandText.value))
const hasDetails = computed(() => {
  if (isGroup.value) return true
  if (isReasoning.value) {
    const text = (props.entry.blocks || []).map((block) => block.map((segment) => segment.text).join('')).join(' ')
      .replace(/^\*{1,2}|\*{1,2}$/g, '').replace(/\s+/g, ' ').trim()
    return Boolean(text && text !== reasoningSummary.value)
  }
  return Boolean(tool.value?.argumentsTruncated || resultMessage.value?.content || errorDetails.value || shellCommandText.value || toolDetails.value.length || props.entry.text?.trim() || props.entry.blocks?.length)
})
const detailKey = computed(() => isTool.value ? `${props.entry.runId || ''}:${props.entry.tool.id || props.entry.key}` : props.entry.key)
const detailsOpen = computed(() => props.expandedDetails.has(detailKey.value))
const detailsLabel = computed(() => `${identity.value.label}: Details ${detailsOpen.value ? 'ausblenden' : 'anzeigen'}`)
watch([detailsOpen, () => props.entry.result, () => props.entry.tool, () => props.entry.batchId], ([open]) => {
  resultLoadError.value = ''
  if (open) loadFullResult()
}, { immediate: true })
async function loadFullResult() {
  const message = resultMessage.value
  const batchId = props.entry.batchId
  const sourceTool = props.entry.tool
  const sourceResult = props.entry.result
  if (!isTool.value || resultLoading.value) return
  const chat = useChatStore()
  const subagents = useSubagentStore()
  const fallbackSessionId = props.readOnly ? subagents.current?.id : chat.current?.id
  const targets = [
    ...(tool.value?.argumentsTruncated ? [{ id: batchId, sessionId: tool.value.sessionId || fallbackSessionId, carrier: true }] : []),
    ...(message?.contentTruncated ? [{ id: message.id, sessionId: message.sessionId || fallbackSessionId }] : [])
  ].filter((target) => target.id && target.sessionId)
  if (!targets.length) return
  resultLoading.value = true
  resultLoadError.value = ''
  try {
    for (const target of targets) {
      try {
        const { data } = await api.get(`/sessions/${target.sessionId}/messages/${target.id}`)
        if (data.id !== target.id) continue
        if (target.carrier
          ? props.entry.batchId !== batchId || props.entry.tool !== sourceTool
          : props.entry.result !== sourceResult) continue
        for (const session of [chat.current, subagents.current]) {
          if (session?.id !== target.sessionId) continue
          const index = session.messages?.findIndex((item) => item.id === target.id) ?? -1
          if (index !== -1) session.messages.splice(index, 1, data)
        }
        if (target.carrier && props.entry.batchId === target.id) hydratedCarrier.value = data
        if (!target.carrier && props.entry.result?.id === target.id) hydratedResult.value = data
      } catch (error) {
        if (props.entry.batchId === batchId && props.entry.result?.id === message?.id) {
          resultLoadError.value = apiErrorMessage(error, 'Die Tool-Details konnten nicht geladen werden.')
        }
      }
    }
  } finally {
    resultLoading.value = false
    if (detailsOpen.value && (props.entry.result !== sourceResult || props.entry.tool !== sourceTool || props.entry.batchId !== batchId)) loadFullResult()
    nextTick(() => emit('resized'))
  }
}
function toggleDetails() {
  if (!hasDetails.value) return
  emit('resize-start')
  emit('toggle-details', detailKey.value)
  nextTick(() => emit('resized'))
}
const groupActive = computed(() => props.entry.active || props.entry.items?.some((item) => item.active))
</script>
