<template>
  <div class="chat-composer" :class="{ 'chat-messages--small': session?.sessionProfile?.messageSize === 'small' }" @pointerdown.capture="updateQueueModifier">
    <q-resize-observer @resize="resizeComposer" />
    <AppMenu
      v-if="paletteOpen" ref="commandMenu" :model-value="paletteOpen" fit no-parent-event no-focus no-refocus
      anchor="top left" self="bottom left" max-height="min(360px, 45dvh)" :max-width="`${composerWidth}px`"
      @update:model-value="!$event && dismissCommands()"
    >
      <CommandList :id="paletteId" ref="commandList" :filter="slashQuery || ''" :disabled="inputDisabled" @selected="selectCommand" @resize="commandMenu?.updatePosition()" />
    </AppMenu>
    <q-card
      flat
      class="app-surface chat-composer__card"
      @mousedown="focusInputFromBackground"
      @dragenter="handleDragEnter"
      @dragover="handleDragOver"
      @dragleave="handleDragLeave"
      @drop="handleDrop"
    >
      <div v-if="fileDragDepth > 0" class="chat-composer__dropzone column flex-center text-primary" role="status">
        <q-icon name="attach_file" size="28px" />
        <span class="text-weight-medium">Dateien hier ablegen</span>
      </div>
      <VoicePanel ref="voicePanel" />
      <q-form v-show="!voice.active" @submit.prevent="submit">
        <q-input
          :key="session?.sessionProfile?.messageSize || 'standard'"
          ref="input"
          :model-value="draftText"
          borderless dense autogrow :autofocus="autofocus" type="textarea" rows="1"
          class="chat-composer__input"
          :class="{ 'chat-composer__input--small': session?.sessionProfile?.messageSize === 'small' }"
          :placeholder="draftHasContent ? undefined : inputPlaceholder"
          :aria-label="inputPlaceholder"
          :disable="inputDisabled || editingQuote !== null"
          role="combobox" aria-autocomplete="list" :aria-expanded="paletteOpen"
          :aria-controls="paletteOpen ? paletteId : undefined" :aria-activedescendant="paletteOpen ? commandList?.activeId : undefined"
          @update:model-value="updateDraft($event || '')"
          @keydown="handleComposerKeydown"
          @keydown.enter.exact.prevent="submit"
          @paste="handlePaste"
        />
        <ComposerAttachments
          :attachments="attachments"
          :disabled="inputDisabled"
          class="q-mb-sm"
          @remove="$emit('remove-attachment', $event)"
        >
          <template v-if="draftQuotes.length" #default>
            <ComposerQuote
              v-for="(quote, index) in draftQuotes" :key="`${draftScope}:${index}`"
              :quote="quote" :disabled="inputDisabled || (editingQuote !== null && editingQuote !== index)"
              :message-size="session?.sessionProfile?.messageSize" class="composer-attachment"
              @update="updateQuote(index, $event)" @remove="removeQuote(index)" @editing="editingQuote = $event ? index : null"
            />
          </template>
        </ComposerAttachments>
        <q-banner
          v-if="currentDictation?.files?.length"
          dense
          rounded
          class="composer-dictation-failed app-surface-tonal q-mb-sm"
        >
          <template #avatar><q-icon name="record_voice_over" color="warning" /></template>
          <div class="text-weight-medium">Noch nicht angehängte Aufnahmen</div>
          <div class="text-caption app-muted">Die Aufnahmen bleiben bis zum Anhängen oder Verwerfen hier erhalten.</div>
          <div v-for="file in currentDictation.files" :key="file.name" class="q-mt-sm">
            <div class="text-caption ellipsis">{{ file.name }}</div>
            <div class="composer-dictation-actions row q-gutter-xs">
              <q-btn flat dense no-caps icon="attach_file" label="Anhängen" :disable="inputDisabled" @click="attachRecording(file, audioScope)" />
              <q-btn flat dense no-caps icon="download" label="Herunterladen" @click="downloadRecording(file)" />
              <q-btn flat dense no-caps color="negative" icon="delete_outline" label="Verwerfen" @click="discardRecording(file)" />
            </div>
          </div>
        </q-banner>
        <div class="chat-composer__footer row items-center">
          <SessionSettingsFields
            ref="settingsFields"
            class="chat-composer__fields"
            :session="session"
            :disabled="inputDisabled"
            :agent-profile-id="session ? chat.activeAgentProfileId : null"
            :fields="['agent', 'model', 'reasoning']"
            compact
            @update="$emit('update', $event)"
            @agent-popup-hide="focusInput()"
          />
          <q-space />
          <input
            ref="attachmentInput"
            class="composer-file-input"
            type="file"
            multiple
            tabindex="-1"
            @change="selectAttachmentFiles"
          >
          <div class="chat-composer__actions row items-center">
            <q-icon
              v-if="sessionSettingsWarning"
              name="warning_amber"
              color="warning"
              size="20px"
              class="q-mr-xs"
              role="img"
              :aria-label="sessionSettingsWarning"
              tabindex="0"
            >
              <q-tooltip>{{ sessionSettingsWarning }}</q-tooltip>
            </q-icon>
            <q-btn
              flat
              round
              dense
              icon="attach_file"
              class="composer-action"
              type="button"
              aria-label="Dateien anhängen"
              :disable="attachmentsDisabled"
              @click="attachmentInput?.click()"
            >
              <q-tooltip>Dateien anhängen</q-tooltip>
            </q-btn>
            <div
              v-if="recording"
              class="composer-recording-status row items-center text-caption text-negative"
              aria-hidden="true"
            >
              <q-icon name="fiber_manual_record" size="12px" class="composer-recording__pulse" />
              <span>{{ recordingDuration }}</span>
            </div>
            <q-btn
              flat round dense icon="headset_mic" class="composer-action" type="button"
              aria-label="Sprachmodus starten"
              :loading="voicePanel?.starting"
              :disable="inputDisabled || voicePanel?.starting || (running && !voicePanel?.canStartWhileRunning) || voiceActivity || chat.activeQueuedMessages.length > 0"
              @click="voicePanel.start()"
            />
            <q-btn
              flat
              round
              dense
              :icon="recording ? 'stop' : 'mic'"
              class="composer-action"
              :color="recording ? 'negative' : undefined"
              type="button"
              :aria-label="recording ? 'Sprachnachricht beenden' : 'Sprachnachricht aufnehmen'"
              :loading="starting || finalizing"
              :disable="inputDisabled || starting || finalizing"
              @click="toggleVoiceRecording"
            >
              <q-tooltip>
                {{ recording
                  ? 'Aufnahme beenden und anhängen'
                  : transcriptionUnavailableReason || 'Sprachnachricht aufnehmen, Transkription beim Senden' }}
              </q-tooltip>
            </q-btn>
            <q-btn
              v-if="running"
              flat
              round
              dense
              class="composer-action"
              color="negative"
              icon="stop"
              aria-label="Agent-Anfrage stoppen"
              type="button"
              :loading="stopping"
              :disable="stopping"
              @click="$emit('stop')"
            />
            <q-btn
              v-if="canSendDuringRun"
              flat
              round
              dense
              class="composer-action"
              :color="queueSend ? 'secondary' : 'primary'"
              :icon="queueSend ? 'schedule_send' : 'arrow_upward'"
              :aria-label="voiceActivity ? voiceSendLabel : queueSend ? 'Nachricht vormerken' : 'Nachricht jetzt senden'"
              type="button"
              :loading="sendingRecording || starting || finalizing"
              :disable="stopping || (voiceActivity && !canFinishAndSend)"
              @click="submit()"
            />
            <q-btn
              v-if="canSendDuringRun && (!voiceActivity || canFinishAndSend)"
              flat
              round
              dense
              icon="arrow_drop_down"
              class="composer-action"
              aria-label="Versandart auswählen"
              type="button"
              :disable="stopping"
            >
              <AppMenu anchor="top right" self="bottom right" auto-close @hide="focusInput()">
                <q-list>
                  <q-item clickable @click="sendMode = 'interrupt'">
                    <q-item-section>Jetzt senden</q-item-section>
                    <q-item-section side><q-icon v-if="sendMode === 'interrupt'" name="check" /></q-item-section>
                  </q-item>
                  <q-item clickable @click="sendMode = 'queue'">
                    <q-item-section>Nach dem Lauf senden</q-item-section>
                    <q-item-section side><q-icon v-if="sendMode === 'queue'" name="check" /></q-item-section>
                  </q-item>
                </q-list>
              </AppMenu>
            </q-btn>
            <q-btn
              v-if="!running"
              flat
              round
              dense
              class="composer-action"
              :color="queueSend ? 'secondary' : 'primary'"
              :icon="queueSend ? 'schedule_send' : 'arrow_upward'"
              :aria-label="voiceActivity ? voiceSendLabel : queueSend ? 'Nachricht vormerken' : 'Nachricht senden'"
              :type="voiceActivity ? 'button' : 'submit'"
              :loading="sendingRecording || starting || finalizing"
              :disable="voiceActivity ? !canFinishAndSend : sendDisabled"
              @click="voiceActivity && submit()"
            />
          </div>
        </div>

      </q-form>
    </q-card>
  </div>
</template>

<script setup>
import AppMenu from 'src/shared/ui/AppMenu.vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { useQuasar } from 'quasar'
import ComposerAttachments from 'src/features/chat/components/ComposerAttachments.vue'
import ComposerQuote from './ComposerQuote.vue'
import { parseQuotedMessage, quotedMessage } from '../quote-drafts'
import SessionSettingsFields from 'src/features/chat/components/SessionSettingsFields.vue'
import { useAgentProfileStore } from 'stores/agent-profiles'
import { useAgentIdentityStore } from 'stores/agent-identity'
import { useChatStore } from 'src/features/chat/store'
import { useProviderStore } from 'stores/providers'
import { useToolSettingsStore } from 'stores/tool-settings'
import { useConfirm } from 'src/composables/confirm'
import { recorderErrorMessage, useAudioRecorder } from 'src/features/chat/audio-recorder'
import { clipboardAttachmentFiles } from 'src/features/chat/attachment-drafts'
import { formatAttachmentDuration } from 'src/features/files/attachments'
import { sessionModel } from '../session-model'
import CommandList from 'src/features/commands/CommandList.vue'
import VoicePanel from 'src/features/voice/VoicePanel.vue'
import { useVoiceStore } from 'src/features/voice/store'

defineOptions({ name: 'ChatComposer' })

const props = defineProps({
  autofocus: { type: Boolean, default: true },
  session: { type: Object, default: null },
  draft: { type: String, default: '' },
  disabled: Boolean,
  running: Boolean,
  stopping: Boolean,
  queueMode: Boolean,
  attachments: { type: Array, default: () => [] }
})
const emit = defineEmits(['add-attachments', 'remove-attachment', 'send', 'stop', 'update', 'update:draft'])
const $q = useQuasar()
const identity = useAgentIdentityStore()
const agentProfiles = useAgentProfileStore()
const chat = useChatStore()
const voice = useVoiceStore()
const voicePanel = ref(null)
const providers = useProviderStore()
const toolSettings = useToolSettingsStore()
const input = ref(null)
const editingQuote = ref(null)
const attachmentInput = ref(null)
const fileDragDepth = ref(0)
const settingsFields = ref(null)
const sendMode = ref('interrupt')
const queueModifier = ref(false)
const queueSend = computed(() => queueModifier.value || (props.running ? sendMode.value === 'queue' : props.queueMode))
const commandList = ref(null)
const commandMenu = ref(null)
const composerWidth = ref(0)
const paletteId = useId()
const draftBlocks = computed(() => parseQuotedMessage(props.draft))
const draftText = computed(() => draftBlocks.value.filter(block => block.type === 'text' && block.text).map(block => block.text).join('\n\n'))
const draftQuotes = computed(() => draftBlocks.value.filter(block => block.type === 'quote'))
const messageContent = computed(() => quotedMessage([{ type: 'text', text: draftText.value }, ...draftQuotes.value]))
const draftScope = computed(() => [chat.draftOwnerUserId, props.session?.id, chat.sessionsProjectId, props.session?.sessionProfile?.messageSize].join(':'))
watch(draftScope, () => { editingQuote.value = null })
const slashDismissed = ref(false)
const slashQuery = computed(() => /^\/[a-z0-9_-]*$/iu.test(draftText.value) ? draftText.value : null)
const paletteOpen = computed(() => !inputDisabled.value && !voiceActivity.value
  && !slashDismissed.value && slashQuery.value !== null)
watch(() => props.draft, () => { slashDismissed.value = false })
watch([() => props.session?.id, () => chat.sessionsProjectId], dismissCommands)
const sendingRecording = ref(false)
const { confirm } = useConfirm()
let recordingScope = null
let finishingRequest = null
let unmounted = false
const {
  starting,
  recording,
  finalizing,
  durationMs: recordingDurationMs,
  startRecording,
  stopRecording,
  cancelRecording
} = useAudioRecorder({
  onAutoStopped(file) {
    retainRecording(file, recordingScope)
    if (!unmounted) {
      $q.notify({ type: 'info', message: 'Die maximale Aufnahmedauer ist erreicht. Die Aufnahme wurde vorbereitet.' })
    }
  }
})
const audioScope = computed(() => ({
  sessionId: props.session?.id || null,
  projectId: chat.sessionsProjectId,
  ownerUserId: chat.draftOwnerUserId
}))
const currentDictation = computed(() => props.session && chat.getAudioDictation(audioScope.value.sessionId, audioScope.value.projectId))
const recordingDuration = computed(() => formatAttachmentDuration(recordingDurationMs.value) || '0:00')
const effectiveModelId = computed(() => sessionModel(props.session, providers, chat.activeAgentProfileId).modelId)
const selectedModelConfig = computed(() => providers.models.find((model) => (
  model.id === effectiveModelId.value
)))
const modelSupportsTools = computed(() => selectedModelConfig.value?.supportsTools !== false)
const transcriptionModelAvailable = computed(() => (
  Boolean(providers.modelDefaults.audioTranscriptionModelId)
  && providers.capabilityOptions('audio_transcription')
    .some((option) => option.value === providers.modelDefaults.audioTranscriptionModelId)
))
const transcriptionUnavailableReason = computed(() => {
  if (toolSettings.loading || !toolSettings.loaded && !toolSettings.loadFailed) {
    return 'Sprachnachricht aufnehmen; Transkriptionseinstellungen werden geladen.'
  }
  if (toolSettings.loadFailed) return 'Sprachnachricht aufnehmen; automatische Transkription derzeit nicht verfügbar.'
  if (!toolSettings.transcribeAudioAvailable) {
    return 'Sprachnachricht aufnehmen; automatische Transkription ist deaktiviert.'
  }
  if (!transcriptionModelAvailable.value) {
    return 'Sprachnachricht aufnehmen; kein Transkriptionsmodell verfügbar.'
  }
  return ''
})
const inputDisabled = computed(() => !props.session || props.disabled)
const attachmentsDisabled = computed(() => inputDisabled.value || starting.value || recording.value || finalizing.value)
watch([attachmentsDisabled, () => props.session?.id, () => chat.sessionsProjectId, () => chat.draftOwnerUserId], () => {
  fileDragDepth.value = 0
})
const draftHasContent = computed(() => Boolean(props.draft.trim() || props.attachments.length))
const inputPlaceholder = computed(() => `Eine Nachricht an ${identity.displayName} senden`)
const voiceActivity = computed(() => starting.value || recording.value || finalizing.value || sendingRecording.value)
const voiceSendLabel = computed(() => queueSend.value ? 'Aufnahme beenden und vormerken' : 'Aufnahme beenden und senden')
const canFinishAndSend = computed(() => (
  recording.value && !inputDisabled.value && !starting.value && !finalizing.value && !sendingRecording.value
  && editingQuote.value === null && modelSupportsTools.value && (!props.running || props.queueMode)
))
const sendDisabled = computed(() => (
  inputDisabled.value
  || editingQuote.value !== null
  || (props.running && !props.queueMode)
  || starting.value
  || recording.value
  || finalizing.value
  || sendingRecording.value
  || !modelSupportsTools.value
  || (!props.draft.trim() && !props.attachments.length)
))
const canSendDuringRun = computed(() => props.running && (voiceActivity.value || (
  props.queueMode && draftHasContent.value && !sendDisabled.value
)))
const sessionSettingsWarning = computed(() => settingsFields.value?.warning || '')

function dismissCommands() {
  slashDismissed.value = true
}

function resizeComposer({ width }) {
  composerWidth.value = width
  void nextTick(() => commandMenu.value?.updatePosition())
}

function selectCommand(command) {
  const argumentsText = draftText.value.replace(/^\/[a-z0-9_-]*(?:\s+|$)/iu, '')
  dismissCommands()
  updateDraft(`/${command.name} ${argumentsText}`)
  void nextTick(() => focusInput())
}

function updateDraft(text, quotes = draftQuotes.value) {
  emit('update:draft', quotedMessage([{ type: 'text', text }, ...quotes]))
}

function updateQuote(index, quote) {
  updateDraft(draftText.value, draftQuotes.value.map((item, position) => position === index ? { ...quote, type: 'quote' } : item))
}

function removeQuote(index) {
  updateDraft(draftText.value, draftQuotes.value.filter((_, position) => position !== index))
}

function focusInput() {
  input.value?.focus()
}

function focusInputFromBackground(event) {
  if (event.button !== 0 || voice.active) return
  if (event.target !== event.currentTarget
    && (!input.value?.$el.contains(event.target) || event.target.tagName === 'TEXTAREA')) return
  event.preventDefault()
  focusInput()
}

async function insertQuote(quote) {
  if (inputDisabled.value || editingQuote.value !== null) return
  updateDraft(draftText.value, [...draftQuotes.value, { ...quote, type: 'quote' }])
  await nextTick()
  focusInput()
}

function submit(event) {
  if (event?.isComposing) return
  if (paletteOpen.value && (commandList.value?.select() || commandList.value?.pending)) return
  if (recording.value) {
    void finishAndSendRecording()
    return
  }
  const content = messageContent.value.trim()
  if (sendDisabled.value) return
  emit('send', content, { interrupt: props.running && !queueSend.value })
}

function updateQueueModifier(event) {
  queueModifier.value = event.ctrlKey || event.metaKey
}

function resetQueueModifier() {
  queueModifier.value = false
}

function handleComposerKeydown(event) {
  if (paletteOpen.value && !event.isComposing && ['ArrowDown', 'ArrowUp', 'Escape', 'Tab'].includes(event.key)) {
    if (event.key === 'Escape') dismissCommands()
    else if (event.key === 'Tab') { if (!commandList.value?.select()) return }
    else commandList.value?.move(event.key === 'ArrowDown' ? 1 : -1)
    event.preventDefault()
    event.stopPropagation()
    return
  }
  if (!event.defaultPrevented && !event.isComposing && !event.altKey && !event.shiftKey
    && (event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault()
    if (!event.repeat) submit(event)
    return
  }
  if (
    event.defaultPrevented || event.repeat || event.isComposing || event.altKey
    || event.ctrlKey === event.metaKey || event.code !== 'Period'
  ) return
  if (event.shiftKey) {
    if (!props.running || props.stopping) return
    sendMode.value = sendMode.value === 'queue' ? 'interrupt' : 'queue'
  } else {
    if (inputDisabled.value || agentProfiles.loading || agentProfiles.agents.length < 2) return
    const agents = agentProfiles.agents.filter((agent) => !props.session?.sessionProfile?.agentIds || props.session.sessionProfile.agentIds.includes(agent.id))
    if (agents.length < 2) return
    const current = chat.activeAgentProfileId || agentProfiles.defaultAgentProfileId
    const index = agents.findIndex((agent) => agent.id === current)
    void chat.updateActive({ agent_profile_id: agents[(index + 1) % agents.length].id }, { draftOnly: true })
  }
  event.preventDefault()
  event.stopPropagation()
}

async function finishAndSendRecording() {
  if (!canFinishAndSend.value || !recordingScope) return
  const scope = recordingScope
  const content = messageContent.value.trim()
  const interrupt = props.running && !queueSend.value
  sendingRecording.value = true
  try {
    const recordedFile = await finishVoiceRecording()
    if (
      !recordedFile || unmounted || scope.ownerUserId !== chat.draftOwnerUserId
      || scope.sessionId !== (chat.current?.id || null)
      || scope.sessionId !== audioScope.value.sessionId
      || scope.projectId !== chat.sessionsProjectId
      || !chat.activeAttachmentDraft.some((attachment) => attachment.file === recordedFile && attachment.voiceMessage)
    ) return
    emit('send', content, { interrupt })
  } finally {
    sendingRecording.value = false
  }
}

function selectAttachmentFiles(event) {
  const files = Array.from(event.target.files || [])
  if (files.length) emit('add-attachments', files)
  event.target.value = ''
}

function handleDragEnter(event) {
  if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return
  event.preventDefault()
  event.stopPropagation()
  if (!attachmentsDisabled.value) fileDragDepth.value += 1
}

function handleDragOver(event) {
  if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return
  event.preventDefault()
  event.stopPropagation()
  event.dataTransfer.dropEffect = attachmentsDisabled.value ? 'none' : 'copy'
}

function handleDragLeave(event) {
  if (!fileDragDepth.value) return
  event.stopPropagation()
  fileDragDepth.value -= 1
}

function handleDrop(event) {
  fileDragDepth.value = 0
  const files = Array.from(event.dataTransfer?.files || [])
  if (!files.length) return
  event.preventDefault()
  event.stopPropagation()
  if (attachmentsDisabled.value) return
  emit('add-attachments', files)
  focusInput()
}

function handlePaste(event) {
  const files = clipboardAttachmentFiles(event)
  if (!files.length) return
  event.preventDefault()
  emit('add-attachments', files)
}

async function startVoiceRecording() {
  if (inputDisabled.value || starting.value || recording.value || finalizing.value) return
  recordingScope = { ...audioScope.value }
  try {
    await startRecording()
  } catch (error) {
    $q.notify({ type: 'negative', message: recorderErrorMessage(error) })
  }
}

function toggleVoiceRecording() {
  return recording.value ? finishVoiceRecording() : startVoiceRecording()
}

function finishVoiceRecording() {
  if (finishingRequest) return finishingRequest
  if (!recording.value) return finalizing.value ? stopRecording().catch(() => {}) : Promise.resolve()
  const scope = recordingScope
  finishingRequest = (async () => {
    try {
      const file = await stopRecording()
      if (file) retainRecording(file, scope)
      return file
    } catch {
      $q.notify({ type: 'negative', message: 'Die Aufnahme konnte nicht verarbeitet werden.' })
    } finally {
      finishingRequest = null
      focusInput()
    }
  })()
  return finishingRequest
}

function retainRecording(file, scope) {
  if (!scope || scope.discarded || scope.ownerUserId !== chat.draftOwnerUserId) return
  const files = chat.getAudioDictation(scope.sessionId, scope.projectId)?.files || []
  if (files.includes(file)) return
  chat.setAudioDictation({ files: [...files, file] }, scope.sessionId, scope.projectId)
  if (!unmounted) attachRecording(file, scope)
}

function removeRecoveryFile(file, scope) {
  const files = (chat.getAudioDictation(scope.sessionId, scope.projectId)?.files || []).filter((item) => item !== file)
  chat.setAudioDictation(files.length ? { files } : null, scope.sessionId, scope.projectId)
}

function attachRecording(file, scope) {
  emit('add-attachments', [file], (result) => {
    if (result?.added === 1 && scope.ownerUserId === chat.draftOwnerUserId) removeRecoveryFile(file, scope)
  }, { ...scope, voiceMessage: true })
}

function downloadRecording(file) {
  if (!file || typeof URL?.createObjectURL !== 'function') return
  const url = URL.createObjectURL(file)
  const link = document.createElement('a')
  link.href = url
  link.download = file.name || 'sprachnachricht.wav'
  link.click()
  URL.revokeObjectURL(url)
}

async function discardRecording(file) {
  const scope = { ...audioScope.value }
  if (!await confirm('Die Aufnahme wird unwiderruflich entfernt.', 'Aufnahme verwerfen?')) return
  if (scope.ownerUserId !== chat.draftOwnerUserId) return
  removeRecoveryFile(file, scope)
  focusInput()
}

async function cancelAudioActivity() {
  if (recordingScope) recordingScope.discarded = true
  await cancelRecording()
}

async function prepareAudioNavigation() {
  if (starting.value) await cancelAudioActivity()
  else await finishVoiceRecording()
}

chat.$onAction(({ name, args, after }) => {
  if (name === 'clearAllAttachmentDrafts') void cancelAudioActivity()
  if (name === 'deleteSession' && args[0] === recordingScope?.sessionId) {
    const scope = recordingScope
    after(() => {
      scope.discarded = true
      if (scope === recordingScope) void cancelAudioActivity()
    })
  }
  if (name === 'removeProjectScope' && args[0] === recordingScope?.projectId) {
    void cancelAudioActivity()
  }
})

watch([() => props.session?.id, () => chat.sessionsProjectId, () => chat.draftOwnerUserId], () => {
  if (recordingScope?.ownerUserId !== chat.draftOwnerUserId) void cancelAudioActivity()
  else void prepareAudioNavigation()
}, { flush: 'sync' })

onMounted(() => {
  window.addEventListener('keydown', updateQueueModifier, true)
  window.addEventListener('keyup', updateQueueModifier, true)
  window.addEventListener('blur', resetQueueModifier)
})

onBeforeUnmount(() => {
  unmounted = true
  window.removeEventListener('keydown', updateQueueModifier, true)
  window.removeEventListener('keyup', updateQueueModifier, true)
  window.removeEventListener('blur', resetQueueModifier)
})

defineExpose({
  focus: focusInput,
  insertQuote,
  quoteEditing: computed(() => editingQuote.value !== null),
  cancelAudioActivity,
  prepareAudioNavigation,
  hasAudioActivity: () => voiceActivity.value
})
</script>
