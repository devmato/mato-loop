<template>
  <div
    :class="[
      'chat-message',
      `chat-message--${message.role}`,
      {
        'chat-message--turn-continues': message.continuesAssistantTurn,
        'chat-message--search-target': highlighted
      }
    ]"
    :data-message-id="message.id"
    :data-render-id="message.renderId || message.id"
    tabindex="-1"
  >
    <span v-if="message.role === 'user'" class="app-sr-only">Benutzernachricht</span>
    <span v-else-if="message.showAgentLabel" class="app-sr-only">Assistentenantwort</span>
    <AgentActivityPanel
      v-if="content || message.workHistory || message.generationStatus === 'interrupted'"
      hide-provided-files
      :history="message.workHistory"
      :subagents="subagents"
      :show-active-subagents="showActiveSubagents"
      :activity="message.currentTurn ? chat.activity : null"
      :presentation="message.currentTurn ? runPresentation : null"
      :reasoning="message.currentTurn ? chat.streamReasoning : null"
      :context-summary="message.currentTurn ? chat.streamContextSummary : null"
      :warnings="message.currentTurn ? chat.runWarnings : emptyItems"
      :trusted-image-origins="trustedImageOrigins"
      :running="chat.running && message.currentTurn"
      :expanded="workHistoryExpanded"
      @open-file="emit('open-file', $event)"
      @history-expanded="emit('history-expanded', message.workHistory?.id || message.id, $event)"
      @rendered="emit('rendered')"
      @resize-start="emit('resize-start')"
      @resized="emit('resized')"
      @trust-image-origin="emit('trust-image-origin', $event)"
    >
      <MessageContent
        v-if="message.preparedReply"
        :content="message.preparedReply.markdown"
        :prepared-replies="message.preparedReply.options"
        class="q-mb-sm"
        @prepared-reply="emit('prepared-reply', $event)"
      />
      <MessageContent
        v-if="content && message.role === 'assistant'"
        class="chat-message__content"
        :content="content"
        :role="message.error ? 'alert' : undefined"
        :file-actions="message.fileActions"
        :research-sources="message.research?.linkedSources || emptyItems"
        :streaming="message.streaming"
        :quotable="canQuote"
        :message-size="messageSize"
        :trusted-image-origins="trustedImageOrigins"
        @open-file="emit('open-file', $event)"
        @rendered="emit('rendered')"
        @trust-image-origin="emit('trust-image-origin', $event)"
        @quote="emit('quote', $event)"
      />
      <div v-else-if="message.quotedContent" class="chat-message__content">
        <template v-for="(block, index) in message.quotedContent" :key="index">
          <ComposerQuote
            v-if="block.type === 'quote'"
            :quote="block"
            :editable="false"
            :message-size="messageSize"
            class="q-my-sm"
          />
          <div v-else-if="block.text">{{ block.text }}</div>
        </template>
      </div>
      <div v-else-if="message.content" class="chat-message__content">{{ message.commandPrompt || message.content }}</div>
    </AgentActivityPanel>
    <ChatFiles
      v-if="message.attachments?.length"
      class="q-mt-sm"
      :attachments="message.attachments"
    />
    <div v-if="message.voice" class="text-caption app-muted row items-center q-gutter-xs">
      <q-icon name="record_voice_over" size="14px" /><span>Sprachgespräch</span>
    </div>
    <FileChanges
      :actions="message.fileChanges"
      :git="git"
      @open-file="emit('open-file', $event)"
      @open-diff="emit('open-diff', $event)"
      @resize-start="emit('resize-start')"
      @resized="emit('resized')"
    />
    <ChatFiles
      :actions="message.providedFiles"
      class="q-my-sm"
      @open-file="emit('open-file', $event)"
    />
    <div
      v-if="message.generationStatus === 'interrupted' && !message.redirected"
      class="app-muted text-caption row items-center q-gutter-x-xs q-mt-sm"
      role="status"
    >
      <q-icon name="cancel" size="14px" aria-hidden="true" />
      <span>{{ message.voice ? 'Sprachbeitrag unterbrochen; der Text kann ungehörte Teile enthalten' : 'Anfrage gestoppt' }}</span>
    </div>
    <div v-if="metadata.visible" class="chat-message__meta items-center wrap">
      <q-btn
        v-if="metadata.canUndo"
        flat
        round
        dense
        size="sm"
        icon="undo"
        class="chat-message__meta-action"
        :class="{ 'chat-message__meta-action--hover': !message.deliveryStatus }"
        :disable="metadata.actionDisabled || metadata.actionLoading"
        aria-label="Nachricht rückgängig machen"
        @click.stop="message.outboxId ? emit('undo-queued', message.outboxId) : emit('undo', message)"
      >
        <q-tooltip>Nachricht rückgängig machen</q-tooltip>
      </q-btn>
      <template v-for="(label, index) in metadata.labels" :key="label.kind">
        <span :class="`chat-message__meta-${label.kind}`" :title="label.title">{{ label.text }}</span>
        <span v-if="index < metadata.labels.length - 1 || message.createdAt" aria-hidden="true">·</span>
      </template>
      <time
        v-if="message.createdAt && !message.deliveryStatus"
        :datetime="message.createdAt"
        :title="metadata.absoluteTime"
        :aria-label="`Gesendet am ${metadata.absoluteTime}`"
      >
        {{ metadata.relativeTime }}
      </time>
      <span
        v-if="message.deliveryStatus"
        class="row inline items-center q-gutter-x-xs"
        :class="{ 'text-negative': message.deliveryStatus === 'failed' }"
        role="status"
      >
        <q-icon :name="metadata.deliveryIcon" size="1em" aria-hidden="true" />
        <span>{{ metadata.deliveryLabel }}</span>
      </span>
      <span v-if="message.queueRecoveryId" class="text-negative" role="status">Lokaler Versand pausiert</span>
      <q-btn
        v-if="metadata.canRecover"
        flat
        round
        dense
        size="sm"
        icon="refresh"
        color="negative"
        class="chat-message__meta-action"
        :aria-label="message.queueRecoveryId ? 'Versand fortsetzen' : 'Nachricht erneut senden'"
        @click.stop="emit('recover')"
      />
      <q-btn
        v-if="metadata.canRetry"
        flat
        round
        dense
        size="sm"
        icon="refresh"
        color="negative"
        class="chat-message__meta-action chat-message__meta-action--retry"
        :loading="metadata.actionLoading"
        :disable="metadata.actionDisabled"
        aria-label="Anfrage erneut starten"
        @click.stop="emit('retry', message)"
      >
        <q-tooltip>Erneut versuchen</q-tooltip>
      </q-btn>
      <q-btn
        v-if="message.role === 'assistant' && content"
        flat round dense size="xs" :icon="copied ? 'check' : 'content_copy'"
        class="chat-message__meta-action chat-message__meta-action--hover q-ml-auto"
        :aria-label="copied ? 'Antwort kopiert' : 'Antwort kopieren'"
        @click.stop="copyAnswer"
      />
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'
import { copyToClipboard, useQuasar } from 'quasar'
import { useAgentProfileStore } from 'stores/agent-profiles'
import { useChatStore } from 'src/features/chat/store'
import { chatMessageText, retryAvailableForMessage } from 'src/features/chat/transcript'
import { formatAbsoluteDateTime, formatRelativeDateTime } from 'src/utils/date-time'
import AgentActivityPanel from './AgentActivityPanel.vue'
import ChatFiles from './ChatFiles.vue'
import ComposerQuote from './ComposerQuote.vue'
import FileChanges from './FileChanges.vue'
import MessageContent from './MessageContent.vue'

const props = defineProps({
  message: { type: Object, required: true },
  subagents: { type: Array, default: () => [] },
  showActiveSubagents: Boolean,
  runPresentation: { type: Object, default: null },
  trustedImageOrigins: { type: Array, default: () => [] },
  workHistoryExpanded: Boolean,
  highlighted: Boolean,
  canQuote: Boolean,
  git: { type: Object, default: null },
  messageActionId: { type: String, default: null },
  timestampNow: { type: Number, required: true }
})
const emit = defineEmits([
  'undo', 'undo-queued', 'retry', 'recover', 'open-file', 'open-diff',
  'quote', 'prepared-reply', 'history-expanded', 'rendered',
  'resize-start', 'resized', 'trust-image-origin'
])
const emptyItems = Object.freeze([])
const chat = useChatStore()
const agentProfiles = useAgentProfileStore()
const $q = useQuasar()
const messageSize = computed(() => chat.activeContext.sessionProfile?.messageSize)
const content = computed(() => chatMessageText(props.message))
const copied = ref(false)
let copyTimer
let disposed = false
onBeforeUnmount(() => {
  disposed = true
  clearTimeout(copyTimer)
})
const metadata = computed(() => {
  const message = props.message
  const labels = []
  if (message.role === 'assistant' && !message.continuesAssistantTurn && message.origin !== 'host'
    && !(chat.running && message.currentTurn)) {
    labels.push({ kind: 'agent', text: agentProfiles.byId(message.agentProfileId)?.name || 'Allgemein' })
  }
  if (message.origin === 'host' || (message.role === 'assistant'
    && Boolean(message.hasAssistantContent || content.value) && Boolean(message.modelName))) {
    const text = message.origin === 'host' ? 'Host' : message.modelName
    labels.push({ kind: 'model', text, title: text })
  }
  if (message.role === 'user') {
    const source = message.sourceType === 'automationRun'
      ? `Automation${message.automationName ? ` · ${message.automationName}` : ''}`
      : message.source?.type === 'endpoint' && message.source.provider === 'telegram' ? 'Telegram' : ''
    if (source) labels.push({ kind: 'source', text: source })
  }
  const firstQueuedId = chat.activeQueuedMessages[0]?.id
  return {
    labels,
    visible: Boolean(message.deliveryStatus || message.queueRecoveryId || message.createdAt || labels.length),
    absoluteTime: formatAbsoluteDateTime(message.createdAt),
    relativeTime: formatRelativeDateTime(message.createdAt, props.timestampNow),
    deliveryIcon: message.deliveryStatus === 'sending' ? 'send'
      : message.deliveryStatus === 'failed' ? 'error_outline' : 'schedule',
    deliveryLabel: message.deliveryStatus === 'sending' ? 'Wird gesendet …'
      : message.deliveryStatus === 'failed' ? (message.deliveryUnconfirmed ? 'Versand unbestätigt' : 'Nicht gesendet') : 'Geplant',
    canUndo: message.role === 'user' && !chat.current?.archived && !String(message.id).startsWith('local-'),
    canRetry: !chat.current?.archived && retryAvailableForMessage(message, chat.current?.messages, chat.running),
    canRecover: (message.deliveryStatus === 'failed' && message.outboxId === firstQueuedId)
      || Boolean(message.queueRecoveryId && message.queueRecoveryId === firstQueuedId),
    actionLoading: props.messageActionId === message.id,
    actionDisabled: chat.loading || chat.queueUndoId !== null
      || (props.messageActionId !== null && props.messageActionId !== message.id)
  }
})

async function copyAnswer() {
  try {
    await copyToClipboard(content.value)
    if (disposed) return
    copied.value = true
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => { copied.value = false }, 1800)
  } catch {
    if (!disposed) $q.notify({ type: 'negative', message: 'Die Antwort konnte nicht kopiert werden.' })
  }
}
</script>
