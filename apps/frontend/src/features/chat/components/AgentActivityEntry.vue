<template>
  <div v-if="entry.type === 'tool-group'" class="work-history__tool-group">
    <q-item dense class="work-history__tool-group-header">
      <q-item-section>
        <q-item-label lines="1" class="work-history__activity-line">
          <q-icon
            :name="groupIdentity.icon"
            size="18px"
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
            {{ groupIdentity.label }}
          </span>
          <span
            class="work-history__activity-summary"
            :aria-label="`${entry.items.length} Aufrufe`"
          >· {{ entry.items.length }}</span>
        </q-item-label>
      </q-item-section>
    </q-item>
    <div class="work-history__tool-group-items">
      <AgentActivityEntry
        v-for="item in entry.items"
        :key="item.key"
         :entry="{ ...item, detailOnly: true }"
        :expanded-details="expandedDetails"
        :trusted-image-origins="trustedImageOrigins"
        :read-only="readOnly"
        :show-output="showOutput"
        :show-supplementary="showSupplementary"
        @open-approval-draft="emit('open-approval-draft', $event)"
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
    <q-item
      v-if="!entry.detailOnly || summary || hasDetails || statusIcon"
      dense
      class="activity-row"
      manual-focus
      :class="isTool ? ['tool-call tool-call__header', `tool-call--${status.name}`, { 'tool-call--detail': entry.detailOnly }] : 'work-history__reasoning-header'"
      :clickable="hasDetails"
      :aria-expanded="hasDetails ? detailsOpen : undefined"
      :aria-controls="hasDetails ? detailsId : undefined"
      :aria-label="hasDetails ? detailsLabel : undefined"
      @click="toggleDetails"
    >
      <q-item-section>
        <q-item-label lines="1" class="work-history__activity-line">
          <q-icon
            v-if="identity.icon && !entry.detailOnly"
            :name="identity.icon"
            size="18px"
            :class="{ 'work-history__activity-title--past': !entry.active }"
            aria-hidden="true"
          />
          <span
            v-if="!entry.detailOnly"
            class="work-history__activity-title ellipsis"
            :class="{ 'tool-call__title': isTool, 'activity-shimmer': entry.active, 'work-history__activity-title--past': !entry.active }"
          >{{ identity.label }}</span>
          <span v-if="summary" class="work-history__activity-summary" :class="{ 'tool-call__summary': isTool }">{{ summary }}</span>
          <q-icon
            v-if="hasDetails"
            :name="detailsOpen ? 'expand_more' : 'chevron_right'"
            size="17px"
            class="work-history__detail-indicator"
            aria-hidden="true"
          />
        </q-item-label>
      </q-item-section>
      <q-item-section v-if="statusIcon || entry.presentation?.duration" side class="tool-call__state">
        <div class="tool-call__status row items-center no-wrap q-gutter-xs" :class="`tool-call__status--${status.name}`">
          <span v-if="entry.presentation?.duration" class="work-history__reasoning-duration">{{ entry.presentation.duration }}</span>
          <q-icon v-if="statusIcon" :name="statusIcon" size="17px" role="img" :aria-label="status.label" />
        </div>
      </q-item-section>
    </q-item>
    <div v-if="hasDetails && detailsOpen" :id="detailsId" class="work-history__reasoning-detail">
      <p v-if="errorDetails" class="tool-call__message" :class="`tool-call__status--${status.name}`" role="status">{{ errorDetails }}</p>
      <template v-if="isTool">
        <pre v-if="shellCommandText" class="activity-row__shell-command q-ma-none">{{ shellCommandText }}</pre>
        <div v-for="(item, index) in toolDetails" :key="index" class="q-mb-sm">
          <div class="text-weight-medium">{{ item.label }}</div>
          <ul v-if="item.format === 'list'" class="q-my-none q-pl-md"><li v-for="(value, i) in item.value" :key="i">{{ value }}</li></ul>
          <pre v-else-if="item.format === 'code'" class="activity-row__code q-ma-none">{{ item.value }}</pre>
          <div v-else>{{ item.value }}</div>
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
    <template v-if="showSupplementary">
      <SubagentRows :children="entry.subagents || []" />
      <ApprovalCard
        v-if="entry.approval && !readOnly"
        :approval="entry.approval"
        class="work-history__attention"
        @open-approval-draft="emit('open-approval-draft', $event)"
      />
      <QuestionDock
        v-if="entry.question && !readOnly"
        :key="entry.question.id"
        :request="entry.question"
        class="work-history__attention"
        @rendered="emit('rendered')"
      />
    </template>
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
import { computed, nextTick, useId } from 'vue'
import ApprovalCard from 'src/features/approvals/ApprovalCard.vue'
import ChatFiles from 'src/features/chat/components/ChatFiles.vue'
import MessageContent from 'src/features/chat/components/MessageContent.vue'
import QuestionDock from 'src/features/questions/QuestionDock.vue'
import SubagentRows from 'src/features/subagents/SubagentRows.vue'
import { parseToolResult, shellCommand, toolIdentity, toolPresentation, toolStatus } from 'src/utils/tool-display'

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
const emit = defineEmits(['open-approval-draft', 'open-file', 'rendered', 'resize-start', 'resized', 'trust-image-origin', 'toggle-details'])
const detailsId = useId()
const isTool = computed(() => props.entry.type === 'tool')
const identity = computed(() => isTool.value
  ? toolIdentity(props.entry.tool)
  : { label: props.entry.presentation?.title, icon: props.entry.presentation?.icon })
const result = computed(() => isTool.value ? parseToolResult(props.entry.result?.content, props.entry.tool.name) : {})
const toolInfo = computed(() => isTool.value ? toolPresentation(props.entry.tool, props.entry.approval?.details?.displayItems) : null)
const summary = computed(() => isTool.value ? result.value.summary || toolInfo.value.summary : props.entry.presentation?.summary)
const shellCommandText = computed(() => isTool.value ? shellCommand(props.entry.tool) : '')
const status = computed(() => isTool.value ? toolStatus(props.entry) : { name: props.entry.warning ? 'interrupted' : '', label: 'Warnung' })
const statusIcon = computed(() => ({ failed: 'error', denied: 'error', interrupted: 'warning', approval: 'pending_actions', question: 'help_outline' })[status.value.name])
const errorDetails = computed(() => result.value.errorMessage || '')
const toolDetails = computed(() => (toolInfo.value?.items || []).filter((item) => item.value.length && item.value !== summary.value && item.value !== shellCommandText.value))
const hasDetails = computed(() => Boolean(errorDetails.value || shellCommandText.value || toolDetails.value.length || props.entry.text?.trim() || props.entry.blocks?.length))
const detailKey = computed(() => isTool.value ? `${props.entry.runId || ''}:${props.entry.tool.id || props.entry.key}` : props.entry.key)
const detailsOpen = computed(() => props.expandedDetails.has(detailKey.value))
const detailsLabel = computed(() => `${identity.value.label}: Details ${detailsOpen.value ? 'ausblenden' : 'anzeigen'}`)
function toggleDetails() {
  if (!hasDetails.value) return
  emit('resize-start')
  emit('toggle-details', detailKey.value)
  nextTick(() => emit('resized'))
}
const groupActive = computed(() => props.entry.active || props.entry.items?.some((item) => item.active))
const groupIdentity = computed(() => toolIdentity(props.entry.tool))
</script>
