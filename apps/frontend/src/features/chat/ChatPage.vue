<template>
  <q-page :style-fn="chatPageStyle" class="chat-page column no-wrap">
    <q-splitter
      v-model="splitPosition"
      class="chat-workspace-splitter col"
      :limits="splitLimits"
      :disable="!desktopControlCenterOpen"
      separator-class="bg-transparent"
      :separator-style="desktopControlCenterOpen
        ? { width: '12px', margin: '0 6px' }
        : { display: 'none' }"
    >
      <template #before>
        <SubagentInspector
          v-if="routeSessionId && routeSubagentId"
          :parent-id="routeSessionId"
          :child-id="routeSubagentId"
        />
        <div v-show="!routeSubagentId" class="chat-page__pane column no-wrap full-height">
    <q-banner v-if="chat.current?.archived" class="app-surface-tonal" dense>
      Diese Session ist archiviert. Zum Weiterarbeiten bitte wiederherstellen.
      <template #action>
        <q-btn v-if="chat.running" flat no-caps label="Lauf stoppen" @click="chat.stop()" />
        <q-btn flat no-caps color="primary" label="Archiv öffnen" :to="{ name: 'archive' }" />
      </template>
    </q-banner>
    <q-scroll-area
      id="chat-scroll-area"
      ref="scrollArea"
      class="chat-page__content col"
      :aria-busy="chat.loading ? 'true' : 'false'"
      @keydown="handleScrollKey"
      @mousedown="handleScrollAreaMouseDown"
    >
      <div ref="scrollContent" class="chat-page__scroll-content" :class="{ 'chat-messages--small': chat.activeContext.sessionProfile?.messageSize === 'small', 'chat-page__scroll-content--mobile-home': $q.screen.lt.md && showHome && !runPresentation.showActivity && !chat.loading && !chat.loadError }">
        <div v-if="chat.loading && !chat.current" class="chat-transcript chat-history-loading q-pa-md" role="status" aria-label="Session wird geladen">
          <div v-for="width in ['42%', '86%', '64%']" :key="width" class="chat-session-skeleton">
            <q-skeleton type="text" width="88px" />
            <q-skeleton type="text" :width="width" height="24px" />
            <q-skeleton type="text" width="72%" />
          </div>
        </div>

        <div
          v-else-if="chat.loadError"
          class="chat-session-state text-center q-pa-lg"
          role="alert"
        >
          <q-icon name="error_outline" color="negative" size="40px" />
          <div class="text-h6 q-mt-sm">Session konnte nicht geladen werden</div>
          <p class="app-muted q-mt-sm">{{ sessionLoadErrorMessage }}</p>
          <q-btn
            unelevated
            no-caps
            color="primary"
            icon="refresh"
            label="Erneut versuchen"
            @click="loadRouteSession"
          />
        </div>

        <HomeContent
          v-else-if="showHome && !runPresentation.showActivity"
        />
        <div v-else-if="isNewConversation && !chat.activeQueuedMessages.length" class="chat-session-state text-center q-pa-lg">
          <template v-if="!runPresentation.showActivity">
            <h1 class="text-h5 text-weight-bold q-mb-sm">Neue Session</h1>
            <p class="text-body1 app-muted">Was möchtest du besprechen?</p>
          </template>
          <AgentActivityPanel
            v-if="runPresentation.showActivity"
            :history="liveWorkHistory"
            :activity="chat.activity"
            :presentation="runPresentation"
            :context-summary="chat.streamContextSummary"
            :warnings="chat.runWarnings"
            :trusted-image-origins="trustedImageOrigins"
            class="text-left"
            running
            @resize-start="beginActivityResize"
            @resized="finishActivityResize"
          />
        </div>

        <q-virtual-scroll
          v-else-if="chat.current || chat.activeQueuedMessages.length"
          ref="virtualScroll"
          class="chat-transcript q-pa-md relative-position"
          :class="{ invisible: transcriptPreparing }"
          :inert="transcriptPreparing"
          :items="displayMessages"
          scroll-target="#chat-scroll-area > .scroll"
          :virtual-scroll-item-size="96"
          @virtual-scroll="handleVirtualScroll"
        >
          <template #before>
            <div v-if="chat.olderMessagesLoading" class="absolute-top q-px-md" role="status">
              <div class="chat-history-loading" aria-hidden="true">
                <q-skeleton type="text" width="42%" height="10px" />
                <q-skeleton type="text" width="72%" height="10px" />
              </div>
              <span class="app-sr-only">Ältere Nachrichten werden geladen</span>
            </div>
            <q-banner
              v-else-if="chat.olderMessagesLoadFailed"
              dense
              rounded
              class="app-surface-tonal q-mb-md"
            >
              Ältere Nachrichten konnten nicht geladen werden.
              <template #action>
                <q-btn flat dense no-caps color="primary" label="Erneut versuchen" @click="loadOlderHistory" />
              </template>
            </q-banner>
          </template>
          <template #default="{ item: message }">
            <div
              :key="message.renderId || message.id"
              class="chat-transcript__item"
              :class="{
                'chat-transcript__item--sending': message.reserveResponseSpace
                  || (message.deliveryStatus === 'sending' && message.outboxId === sendingMessageId)
              }"
            >
              <ChatMessage
                :message="message"
                :subagents="childrenByMessage.get(message.id) || emptyItems"
                :show-active-subagents="message.id === latestAssistantMessageId"
                :run-presentation="runPresentation"
                :trusted-image-origins="trustedImageOrigins"
                :work-history-expanded="expandedWorkHistoryIds.has(message.workHistory?.id || message.id)"
                :highlighted="highlightedMessageId === message.id"
                :can-quote="canQuote"
                :git="currentGit"
                :message-action-id="messageActionId"
                :timestamp-now="messageTimestampNow"
                @undo="undoMessage"
                @undo-queued="undoQueuedMessage"
                @retry="retryMessage"
                @recover="chat.resumeQueuedSend()"
                @open-file="openToolFile"
                @open-diff="openGitDiff"
                @quote="addQuote"
                @prepared-reply="sendPreparedAnswer"
                @history-expanded="setWorkHistoryExpanded"
                @rendered="handleMessageRendered"
                @resize-start="beginActivityResize"
                @resized="finishActivityResize"
                @trust-image-origin="trustImageOrigin"
              />
            </div>
          </template>
          <template #after>
            <q-banner v-if="subagents.childrenError" rounded class="app-surface-tonal q-mt-md" role="alert">
              {{ apiErrorMessage(subagents.childrenError) }}
              <template #action>
                <q-btn flat no-caps color="primary" label="Subagenten laden" @click="subagents.loadChildren()" />
              </template>
            </q-banner>
            <SubagentRows :children="unmatchedChildren" />
            <RequestDock
              :key="chat.current?.id"
              :approvals="sessionApprovals"
              :questions="sessionQuestions"
              class="q-mt-md"
              @open-approval-draft="openApprovalDraft"
              @rendered="handleMessageRendered"
            />
          </template>
        </q-virtual-scroll>
      </div>
    </q-scroll-area>

    <div
      v-if="!chat.loadError && !chat.current?.readOnly && !chat.current?.archived && (chat.loading || !routeSessionId || chat.current)"
      class="chat-composer-region"
    >
      <transition name="chat-scroll-jump">
        <div v-if="chat.current && !followingBottom" class="chat-scroll-to-bottom">
          <q-btn
            round
            unelevated
            icon="keyboard_arrow_down"
            class="chat-scroll-to-bottom__button"
            aria-label="Automatisches Mitscrollen aktivieren"
            @click="enableFollowMode"
          >
            <q-tooltip>Automatisches Mitscrollen aktivieren</q-tooltip>
          </q-btn>
        </div>
      </transition>

      <q-banner
        v-if="chat.outboxLoadFailed"
        dense
        rounded
        class="app-surface-tonal q-mx-md q-mt-sm"
      >
        Der lokale Nachrichtenspeicher konnte nicht geladen werden.
        <template #action>
          <q-btn flat dense no-caps label="Erneut versuchen" @click="chat.loadQueuedMessages()" />
        </template>
      </q-banner>
      <div class="chat-input-dock q-pa-md" :class="{ 'chat-input-dock--with-todos': chat.current?.todos?.length }">
        <h2 v-if="showHome" class="text-weight-medium q-mt-none q-mb-sm" :class="$q.screen.lt.sm ? 'text-subtitle2' : 'text-subtitle1'">Neue Session</h2>
        <ComputerChoiceStrip
          v-if="isNewConversation && !chat.activeQueuedMessages.length"
          :session="chat.activeContext"
          :disabled="draftProjectDisabled"
          class="q-mb-sm"
          @update="chat.updateActive"
        />
        <SessionSettingsFields
          v-if="isNewConversation && !chat.activeQueuedMessages.length"
          :session="chat.activeContext"
          :disabled="draftProjectDisabled"
          :fields="['memory']"
          compact
          equal-fields
          class="q-mb-sm"
          @update="chat.updateActive"
        >
          <template #before>
            <div class="chat-start-project row no-wrap items-center">
              <AppSelect
                id="draft-project-select"
                :model-value="routeProjectId"
                :options="draftProjectOptions"
                :disable="draftProjectDisabled"
                :loading="projects.loading || changingDraftProject"
                :clearable="Boolean(routeProjectId)"
                label="Projekt"
                borderless dense stack-label emit-value map-options options-dense hide-bottom-space
                behavior="menu"
                class="draft-project-select q-px-sm text-body2 col"
                aria-label="Projekt wählen oder wechseln"
                @popup-show="loadDraftProjects"
                @update:model-value="changeDraftProject"
              >
                <template #prepend><q-icon name="folder_open" size="18px" /></template>
                <template #selected><span class="ellipsis">{{ draftProjectLabel }}</span></template>
                <template #append>
                  <q-btn
                    flat round dense icon="add"
                    :disable="draftProjectDisabled"
                    aria-label="Neues Projekt erstellen"
                    @click.stop="openDraftProjectCreation"
                  />
                </template>
              </AppSelect>
            </div>
            <WorkspaceField :session="chat.activeContext" :project-id="routeProjectId" :disabled="draftProjectDisabled" @update="chat.updateActive" />
            <AppSelect :model-value="chat.activeContext.sessionTemplateId || templates.defaultTemplateId" :options="templates.templates.map((item) => ({ label: `${item.name}${item.scope === 'personal' ? ' (Persönlich)' : ''}`, value: item.id }))" :disable="draftProjectDisabled" :loading="templates.loading" label="Sessionvorlage" borderless dense stack-label emit-value map-options hide-bottom-space class="chat-start-select q-px-sm text-body2" aria-label="Sessionvorlage auswählen" @update:model-value="changeTemplate">
              <template #prepend><q-icon name="dashboard_customize" size="18px" /></template>
            </AppSelect>
          </template>
        </SessionSettingsFields>
        <SessionTodoStrip
          :todos="chat.current?.todos || emptyItems"
          :collapsed="chat.current?.todosCollapsed || false"
          :saving="savingTodos"
          @update:collapsed="setTodosCollapsed"
        />
        <ChatComposer
          ref="composer"
          :autofocus="!chat.loading && !showHome"
          :session="chat.loading ? null : chat.activeContext"
          :draft="chat.loading || pendingDraft ? '' : chat.activeDraft"
          :disabled="chat.loading || changingDraftProject || !chat.outboxLoaded
            || chat.outboxSaving
            || chat.historyActionPending || Boolean(chat.queueUndoId)
            || (!chat.current && chat.activeQueuedMessages.length > 0)"
          :running="!chat.loading && chat.running"
          :stopping="chat.stopping"
          :queue-mode="chat.messageQueueActive"
          :attachments="chat.loading || pendingDraft ? emptyItems : chat.activeAttachmentDraft"
          @add-attachments="addAttachmentFiles"
          @remove-attachment="chat.removeAttachmentFile"
          @send="send"
          @stop="chat.stop"
          @update="chat.updateActive"
          @update:draft="chat.setDraft($event, { defer: true })"
        />
      </div>
    </div>
    <span class="app-sr-only" role="status" aria-live="polite" aria-atomic="true">
      {{ announcedRunStatus }}
    </span>
        </div>
      </template>

      <template #separator>
        <div
          v-if="desktopControlCenterOpen"
          class="chat-workspace-splitter__handle flex flex-center"
          role="separator"
          aria-label="Breite des Chat-Bereichs ändern"
          aria-orientation="vertical"
          :aria-valuemin="MIN_CONTROL_CENTER_SPLIT_RATIO"
          :aria-valuemax="MAX_CONTROL_CENTER_SPLIT_RATIO"
          :aria-valuenow="controlCenter.splitRatio"
          :aria-valuetext="`${controlCenter.splitRatio} Prozent Chat, ${100 - controlCenter.splitRatio} Prozent Control Center`"
          tabindex="0"
          @keydown="resizeWorkspacePane"
        >
          <q-icon name="drag_indicator" size="18px" />
        </div>
      </template>

      <template #after>
        <aside
          v-if="desktopControlCenterOpen"
          class="chat-workspace-pane full-height"
          aria-label="Control Center"
        >
          <ControlCenterPanel />
        </aside>
      </template>
    </q-splitter>
    <SessionTabs v-if="sessionTabsEnabled" enabled :tabs="sessionTabs" :active-key="activeSessionTabKey" :position="sessionTabsPosition" />
    <SkillDraftPreview ref="skillDraftPreview" />
    <ProjectDialog v-if="draftProjectDialogOpen" v-model="draftProjectDialogOpen" @created="selectCreatedDraftProject" />
  </q-page>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useRoute, useRouter } from 'vue-router'
import RequestDock from 'src/features/chat/components/RequestDock.vue'
import SkillDraftPreview from 'src/features/approvals/SkillDraftPreview.vue'
import AgentActivityPanel from 'src/features/chat/components/AgentActivityPanel.vue'
import ChatMessage from 'src/features/chat/components/ChatMessage.vue'
import HomeContent from 'src/features/home/HomeContent.vue'
import ComputerChoiceStrip from 'src/features/chat/components/ComputerChoiceStrip.vue'
import ProjectDialog from 'src/features/projects/ProjectDialog.vue'
import ChatComposer from 'src/features/chat/components/ChatComposer.vue'
import { parseQuotedMessage } from './quote-drafts'
import { useChatViewport } from './useChatViewport'
import { useChatNavigation } from './useChatNavigation'
import { useSessionTabs } from './useSessionTabs'
import SessionTabs from './components/SessionTabs.vue'
import SessionTodoStrip from 'src/features/chat/components/SessionTodoStrip.vue'
import SessionSettingsFields from 'src/features/chat/components/SessionSettingsFields.vue'
import WorkspaceField from 'src/features/coding/WorkspaceField.vue'
import ControlCenterPanel from 'src/app/control-center/ControlCenterPanel.vue'
import SubagentInspector from 'src/features/subagents/SubagentInspector.vue'
import SubagentRows from 'src/features/subagents/SubagentRows.vue'
import { isActiveSubagent, useSubagentStore } from 'stores/subagents'
import { useAgentProfileStore } from 'stores/agent-profiles'
import { useApprovalStore } from 'stores/approvals'
import { useChatStore } from 'src/features/chat/store'
import {
  MAX_CONTROL_CENTER_SPLIT_RATIO,
  MIN_CONTROL_CENTER_SPLIT_RATIO,
  useControlCenterStore
} from 'stores/control-center'
import { useExecutionAgentStore } from 'stores/execution-agents'
import { useIntegrationStore } from 'stores/integrations'
import { useMailStore } from 'stores/mail'
import { useProviderStore } from 'stores/providers'
import { useProjectStore } from 'stores/projects'
import { useSessionTemplateStore } from 'src/features/settings/session-templates-store'
import { useQuestionStore } from 'stores/questions'
import { useToolSettingsStore } from 'stores/tool-settings'
import { useInstanceLimitsStore } from 'stores/instance-limits'
import { useWorkspaceStore } from 'stores/workspace'
import { gitContext, gitContextKey, useGitChangesStore } from 'src/features/files/git-store'
import { useConfirm } from 'src/composables/confirm'
import { agentRunPresentation } from 'src/features/chat/agent-activity'
import {
  buildChatTranscript,
  withQueuedTranscriptMessages
} from 'src/features/chat/transcript'
import { apiErrorMessage } from 'src/utils/api-errors'
import { notifyError } from 'src/utils/notifications'

defineOptions({ name: 'ChatPage' })

const emptyItems = Object.freeze([])
const route = useRoute()
const router = useRouter()
const $q = useQuasar()
const { confirm } = useConfirm()
const chat = useChatStore()
const { enabled: sessionTabsEnabled, tabs: sessionTabs, position: sessionTabsPosition, activeKey: activeSessionTabKey } = useSessionTabs({ chat, route })
const subagents = useSubagentStore()
const agentProfiles = useAgentProfileStore()
const approvals = useApprovalStore()
const questions = useQuestionStore()
const executionAgents = useExecutionAgentStore()
const instanceLimits = useInstanceLimitsStore()
const integrations = useIntegrationStore()
const mail = useMailStore()
const providers = useProviderStore()
const projects = useProjectStore()
const templates = useSessionTemplateStore()
async function changeTemplate(id) {
  try { await chat.updateActive({ session_template_id: id }) } catch { /* Central API toast. */ }
}
const toolSettings = useToolSettingsStore()
const controlCenter = useControlCenterStore()
const workspace = useWorkspaceStore()
const git = useGitChangesStore()
const currentGitContext = computed(() => {
  const tab = controlCenter.activeTab
  return gitContext(chat.current, tab?.fileTarget, git.current, controlCenter.fileDiffModes[tab?.tabId])
})
const gitAvailable = computed(() => {
  if (!currentGitContext.value) return false
  const agent = executionAgents.byId(currentGitContext.value.executionAgentId)
  return Boolean(agent?.connected && agent.operations?.includes('git.changes'))
})
const currentGit = computed(() => {
  const state = git.current
  return gitAvailable.value && state?.repository && !state.error
    ? state
    : null
})
watch([currentGit, () => controlCenter.gitAutoAdded, () => chat.loading], ([state]) => {
  if (chat.loading || !chat.current?.projectId || state?.currentDirectory !== chat.current.currentDirectory) return
  void controlCenter.autoAddGit(state?.repository).catch(() => {})
})
watch([
  () => gitContextKey(currentGitContext.value), gitAvailable, () => chat.running
], ([key, available, running], [oldKey, oldAvailable, oldRunning] = []) => {
  git.setContext(available ? currentGitContext.value : null)
  if (available && (key !== oldKey || !oldAvailable || (!running && oldRunning))) void git.refresh()
}, { immediate: true })

async function openGitDiff(path) {
  const state = git.current
  if (!state?.repository) return
  try {
    if (path) return await git.openFile(path)
    await git.openChanges()
  } catch { /* Central API toast. */ }
}
const scrollArea = ref(null)
const scrollContent = ref(null)
const virtualScroll = ref(null)
const composer = ref(null)
const savingTodos = ref(false)
async function setTodosCollapsed(collapsed) {
  if (!chat.current || savingTodos.value) return
  savingTodos.value = true
  try {
    await chat.setTodosCollapsed(chat.current.id, collapsed)
  } catch {
    // The API interceptor reports failures; keep the confirmed state.
  } finally {
    savingTodos.value = false
  }
}
const trustedImageOriginsBySession = ref(new Map())
const expandedWorkHistoryIds = ref(new Set())
const pendingDraft = computed(() => chat.activePendingMessage && !chat.activePendingMessage.preserveDraft)
const canQuote = computed(() => Boolean(composer.value) && !composer.value?.quoteEditing && !chat.loading && !pendingDraft.value
  && !changingDraftProject.value && chat.outboxLoaded && !chat.outboxSaving && !chat.historyActionPending)
const sendingMessageId = computed(() => chat.queueSendingEntryId || (
  chat.activity.phase === 'submitting' || chat.activePendingMessage?.continuationRunId
    ? chat.activePendingMessage?.id : null
))
const messageTimestampNow = ref(Date.now())
let messageTimestampTimer, runAnnouncementTimer
const messageActionId = ref(null)
const skillDraftPreview = ref(null)
const announcedRunStatus = ref('')
const liveWorkHistory = computed(() => ({
  entries: [],
  observedDurationMs: null,
  activeDurationMs: chat.activity.activeDurationMs,
  activeDurationUpdatedAt: chat.activity.activeDurationUpdatedAt
}))
function setWorkHistoryExpanded(messageId, expanded) {
  const next = new Set(expandedWorkHistoryIds.value)
  if (expanded) next.add(messageId)
  else next.delete(messageId)
  expandedWorkHistoryIds.value = next
}
const { routeSessionId, routeProjectId, routeSubagentId, newSessionRequested, loadRouteSession } = useChatNavigation({
  chat, route, router, composer, controlCenter, subagents, projects
})
const isNewConversation = computed(() => !routeSessionId.value && !chat.current && !chat.loading && !chat.loadError)
const showHome = computed(() => isNewConversation.value && !newSessionRequested.value && !chat.activeQueuedMessages.length)
const draftProjectLabel = computed(() => routeProjectId.value
  ? projects.items.find(project => project.id === routeProjectId.value)?.name || 'Projekt'
  : 'Ohne Projekt')
const changingDraftProject = ref(false)
const draftProjectDialogOpen = ref(false)
let draftProjectCreationContext = null
const draftProjectOptions = computed(() => [
  { label: 'Ohne Projekt', value: null },
  ...projects.items.map(project => ({ label: project.name, value: project.id }))
])
const draftProjectDisabled = computed(() => changingDraftProject.value || !isNewConversation.value
  || routeProjectId.value !== chat.sessionsProjectId || !chat.outboxLoaded
  || chat.running || chat.outboxSaving
  || chat.activeQueuedMessages.length > 0 || Boolean(chat.emptySessionPromise || chat.queueDrainPromise))
const sessionApprovals = computed(() => !chat.loading && chat.current?.id === routeSessionId.value
  ? approvals.forSession(chat.current.id) : [])
const sessionQuestions = computed(() => !chat.loading && chat.current?.id === routeSessionId.value
  ? questions.forSession(chat.current.id) : [])
const activeQuestion = computed(() => sessionQuestions.value[0] || null)
const transcript = computed(() => buildChatTranscript(
  chat.current?.messages,
  sessionApprovals.value,
  {
    running: chat.running && chat.activity.phase !== 'submitting',
    runId: chat.runId || chat.current?.activeRun?.id,
    toolExecutionActive: chat.activity.phase === 'processing_tool',
    activeToolCallId: chat.activity.toolCallId,
    completedToolCallId: chat.completedToolCallId,
    processOutputs: chat.processOutputs,
    questions: sessionQuestions.value,
    contextSummaries: chat.current?.contextSummaries,
    liveMessage: chat.running && chat.activity.phase !== 'submitting' ? {
      id: `live-${chat.runId}`,
      role: 'assistant',
      userMessageId: chat.streamUserMessageId,
      content: chat.streamContent,
      modelName: chat.streamModelName,
      research: { linkedSources: chat.streamResearchSources },
      agentProfileId: chat.runAgentProfileId || chat.current?.activeRun?.agentProfileId || null,
      streaming: Boolean(chat.streamContent),
      workHistory: liveWorkHistory.value
    } : null
  }
))
const latestAssistantMessageId = computed(() => (
  transcript.value.messages.findLast((message) => message.role === 'assistant')?.id
))
const childrenByMessage = computed(() => {
  const result = new Map()
  for (const message of transcript.value.messages) {
    const children = subagents.children.filter((child) => (
      (message.id === latestAssistantMessageId.value && isActiveSubagent(child))
      || (message.workHistory?.entries || []).some((entry) => (
        entry.tool?.id === child.spawnedByToolCallId && entry.runId === child.spawnedByRunId
      ))
    ))
    if (children.length) result.set(message.id, children)
  }
  return result
})
const unmatchedChildren = computed(() => {
  const matched = new Set([...childrenByMessage.value.values()].flat().map((child) => child.id))
  return subagents.children.filter((child) => isActiveSubagent(child) && !matched.has(child.id))
})
const displayMessages = computed(() => withQueuedTranscriptMessages(
  transcript.value.messages, chat.activeQueuedMessages, chat.queueSendingEntryId, chat.activePendingMessage?.id
).map(message => {
  if (message.role !== 'user') return message
  const blocks = parseQuotedMessage(message.commandPrompt || message.content || '')
  return blocks.some(block => block.type === 'quote') ? { ...message, quotedContent: blocks } : message
}))
const runPresentation = computed(() => agentRunPresentation({
  runId: chat.runId || chat.current?.activeRun?.id,
  running: chat.running,
  stopping: chat.stopping,
  activity: chat.activity,
  streamReasoning: chat.streamReasoning,
  streamContextSummary: chat.streamContextSummary,
  streamContent: chat.streamContent,
  transcript: transcript.value
}))
const trustedImageOrigins = computed(() => (
  trustedImageOriginsBySession.value.get(chat.current?.id) || emptyItems
))
const {
  followingBottom, transcriptPreparing, highlightedMessageId,
  scheduleTranscriptLayout, loadOlderHistory, enableFollowMode,
  beginActivityResize, finishActivityResize, handleMessageRendered,
  handleVirtualScroll, handleScrollKey, handleScrollAreaMouseDown, scrollToTop
} = useChatViewport({
  chat, route, scrollArea, scrollContent, virtualScroll,
  displayMessages, setWorkHistoryExpanded
})
const sessionLoadErrorMessage = computed(() => (
  apiErrorMessage(chat.loadError, 'Die Session konnte nicht geladen werden.')
))
const desktopControlCenterOpen = computed(() => controlCenter.open && $q.screen.gt.sm)
const splitLimits = computed(() => (
  desktopControlCenterOpen.value
    ? [MIN_CONTROL_CENTER_SPLIT_RATIO, MAX_CONTROL_CENTER_SPLIT_RATIO]
    : [100, 100]
))
const splitPosition = computed({
  get: () => desktopControlCenterOpen.value ? controlCenter.splitRatio : 100,
  set: (value) => {
    if (desktopControlCenterOpen.value) controlCenter.setSplitRatio(value)
  }
})

function resizeWorkspacePane(event) {
  let ratio = controlCenter.splitRatio
  if (event.key === 'ArrowLeft') ratio -= 2
  else if (event.key === 'ArrowRight') ratio += 2
  else if (event.key === 'Home') ratio = MIN_CONTROL_CENTER_SPLIT_RATIO
  else if (event.key === 'End') ratio = MAX_CONTROL_CENTER_SPLIT_RATIO
  else return
  event.preventDefault()
  controlCenter.setSplitRatio(Math.min(
    MAX_CONTROL_CENTER_SPLIT_RATIO,
    Math.max(MIN_CONTROL_CENTER_SPLIT_RATIO, ratio)
  ))
}

async function openToolFile(action) {
  const sessionId = action?.sessionId
  if (!sessionId || chat.current?.id !== sessionId) return
  const target = await workspace.resolveFileTarget(action)
  if (target && chat.current?.id === sessionId) await controlCenter.openFile(target)
}

function openApprovalDraft(approval) {
  return skillDraftPreview.value?.open(approval)
}

function trustImageOrigin(origin) {
  const sessionId = chat.current?.id
  if (!sessionId || trustedImageOrigins.value.includes(origin)) return
  const next = new Map(trustedImageOriginsBySession.value)
  next.set(sessionId, [...trustedImageOrigins.value, origin])
  trustedImageOriginsBySession.value = next
}

async function addQuote(quote) {
  if (!canQuote.value) return
  await composer.value?.insertQuote(quote)
}

function confirmHistoryUndo(running) {
  const runningHint = running ? ' Die laufende Anfrage wird dabei abgebrochen.' : ''
  return confirm(
    `Diese Nachricht und alle späteren Chatnachrichten werden entfernt.${runningHint} `
      + 'Der aktuelle Entwurf, seine Anhänge und ein vorhandenes Diktat werden ersetzt. '
      + 'Bereits ausgeführte Tool-, Datei-, Berechtigungs- und Memory-Aktionen bleiben bestehen.',
    'Nachricht rückgängig machen?',
    { okLabel: 'Rückgängig machen' }
  )
}

function undoBlockedByAudio() {
  if (!composer.value?.hasAudioActivity?.()) return false
  $q.notify({
    type: 'info',
    message: 'Beende oder brich zuerst die laufende Aufnahme beziehungsweise Transkription ab.'
  })
  return true
}

async function undoMessage(message) {
  if (undoBlockedByAudio() || !await confirmHistoryUndo(chat.running)) return
  messageActionId.value = message.id
  try {
    await chat.undoUserMessage(message)
    await nextTick()
    composer.value?.focus()
  } finally {
    messageActionId.value = null
  }
}

async function undoQueuedMessage(id) {
  if (undoBlockedByAudio()) return
  const key = chat.activeQueueKey
  const pending = chat.activePendingMessage
  const hasDraft = (pending?.id !== id || pending.preserveDraft) && Boolean(
    chat.activeDraft.trim()
    || chat.activeAttachmentDraft.length
    || chat.getAudioDictation(chat.current?.id || null)
  )
  const restored = await chat.restoreQueuedMessage(id, key, {
    confirmDraftUndo: hasDraft ? () => confirm(
      'Der aktuelle Entwurf, seine Anhänge und ein vorhandenes Diktat werden ersetzt.',
      'Nachricht zurück in den Entwurf holen?',
      { okLabel: 'Rückgängig machen' }
    ) : null,
    confirmSentUndo: (session) => confirmHistoryUndo(Boolean(session.activeRun || (chat.current?.id === session.id && chat.running)))
  })
  if (!restored) return
  await nextTick()
  composer.value?.focus()
}

async function retryMessage(message) {
  const accepted = await confirm(
    'Die Anfrage wird erneut gestartet. Bereits ausgeführte Aktionen werden nicht rückgängig gemacht '
      + 'und können erneut ausgeführt werden.',
    'Antwort erneut versuchen?',
    { okLabel: 'Erneut versuchen', okColor: 'primary' }
  )
  if (!accepted) return
  const sessionId = chat.current?.id
  enableFollowMode()
  messageActionId.value = message.id
  try {
    const result = await chat.retryFailedMessage(message)
    if (!result || chat.current?.id !== sessionId) return
    await nextTick()
    scheduleTranscriptLayout()
  } finally {
    messageActionId.value = null
  }
}

async function loadDraftProjects() {
  if (projects.loading || projects.loaded && !projects.loadFailed) return
  try {
    await projects.fetchAll()
  } catch {
    // The central API interceptor displays the error; reopening retries the list.
  }
}

function openDraftProjectCreation() {
  if (draftProjectDisabled.value) return
  draftProjectCreationContext = { path: route.fullPath, ownerUserId: chat.draftOwnerUserId }
  draftProjectDialogOpen.value = true
}

async function selectCreatedDraftProject(project) {
  const context = draftProjectCreationContext
  draftProjectCreationContext = null
  if (!context || context.path !== route.fullPath || context.ownerUserId !== chat.draftOwnerUserId) return
  await changeDraftProject(project.id)
}

async function changeDraftProject(projectId) {
  if (draftProjectDisabled.value || projectId === chat.sessionsProjectId) return
  const sourceProjectId = chat.sessionsProjectId
  const ownerUserId = chat.draftOwnerUserId
  changingDraftProject.value = true
  chat.sessionSettingsPending += 1
  try {
    await composer.value?.prepareAudioNavigation?.()
    if (!isNewConversation.value || chat.sessionsProjectId !== sourceProjectId || chat.draftOwnerUserId !== ownerUserId) return
    let carryDraft = chat.hasNewSessionDraft(sourceProjectId)
    if (carryDraft && chat.hasNewSessionDraft(projectId)) {
      if (!await confirm(
        'Im Ziel gibt es bereits einen Entwurf oder vorgemerkte Nachrichten. Dein aktueller Entwurf bleibt im bisherigen Bereich erhalten. Möchtest du den vorhandenen Stand öffnen?',
        'Vorhandener Entwurf', { okLabel: 'Vorhandenen öffnen', okColor: 'primary' }
      )) return
      carryDraft = false
    }
    if (!isNewConversation.value || chat.sessionsProjectId !== sourceProjectId || chat.draftOwnerUserId !== ownerUserId) return
    const query = { ...route.query }
    if (projectId) query.project = projectId
    else delete query.project
    if (await router.replace({ name: 'chat', query })) return
    if (!isNewConversation.value || routeProjectId.value !== projectId || chat.draftOwnerUserId !== ownerUserId) return
    await chat.setSessionScope(projectId).catch(() => {})
    if (!isNewConversation.value || routeProjectId.value !== projectId || chat.draftOwnerUserId !== ownerUserId) return
    if (carryDraft && !chat.moveNewSessionDraft(sourceProjectId, projectId)) {
      $q.notify({ type: 'info', message: 'Der vorhandene Entwurf wurde geöffnet. Dein bisheriger Entwurf bleibt erhalten.' })
    }
  } catch (error) {
    notifyError(error)
  } finally {
    changingDraftProject.value = false
    chat.sessionSettingsPending = Math.max(0, chat.sessionSettingsPending - 1)
  }
}

async function loadBackgroundData() {
  const [providerResult, , integrationResult, , , , defaultsResult] = await Promise.allSettled([
    providers.fetchEffective(),
    templates.load(),
    integrations.fetchEffectiveSearch(),
    mail.fetchAccounts(),
    approvals.fetchPending(),
    questions.fetchPending(),
    chat.fetchDefaults(),
    toolSettings.fetchAvailability(),
    instanceLimits.fetchSettings(),
    agentProfiles.fetchAgents()
  ])
  if (
    providerResult.status === 'fulfilled'
    && providers.lastLoadComplete
    && integrationResult.status === 'fulfilled'
    && defaultsResult.status === 'fulfilled'
  ) {
    await chat.reconcileNewSession(
      providers.selectionOptions(null, true)
        .filter((option) => !option.disable)
        .map((option) => option.value),
      integrations.search.effectiveEnabled
    ).catch(() => {})
  }
}

async function send(content, options) {
  enableFollowMode()
  try {
    await chat.send(content, options)
  } finally {
    await nextTick()
    composer.value?.focus()
  }
}

async function sendPreparedAnswer(content) {
  enableFollowMode()
  await chat.send(content, { preserveDraft: true })
}

function addAttachmentFiles(files, done, options) {
  const result = chat.addAttachmentFiles(files, options)
  if (result.error) $q.notify({ type: 'negative', message: result.error })
  done?.(result)
}

function chatPageStyle(offset, height) {
  return {
    minHeight: '0px',
    height: `calc(var(--app-visual-viewport-height, ${height}px) - ${offset}px)`,
    transform: 'translateY(var(--app-visual-viewport-offset-top, 0px))'
  }
}

watch([newSessionRequested, routeProjectId], async ([requested]) => {
  if (!requested || !isNewConversation.value) return
  await nextTick()
  composer.value?.focus()
})
watch(showHome, async (shown) => {
  if (!shown) return
  await nextTick()
  scrollToTop()
})
watch(
  () => runPresentation.value.announcement,
  (value) => {
    clearTimeout(runAnnouncementTimer)
    if (!value) {
      announcedRunStatus.value = ''
      return
    }
    runAnnouncementTimer = window.setTimeout(() => {
      announcedRunStatus.value = value
    }, 250)
  },
  { immediate: true }
)
watch(
  [activeQuestion, () => chat.running],
  async ([question, running], [previousQuestion, wasRunning]) => {
    if (question || running || (!previousQuestion && !wasRunning)) return
    await nextTick()
    composer.value?.focus()
  }
)
watch(
  () => chat.queuedSendReady,
  (ready) => {
    if (ready) void chat.drainOutbox()
  },
  { immediate: true }
)
onMounted(() => {
  messageTimestampTimer = window.setInterval(() => {
    messageTimestampNow.value = Date.now()
  }, 60_000)
  void loadBackgroundData()
  executionAgents.startPolling()
})
onBeforeUnmount(() => {
  draftProjectCreationContext = null
  window.clearInterval(messageTimestampTimer)
  executionAgents.stopPolling()
  clearTimeout(runAnnouncementTimer)
})
</script>
