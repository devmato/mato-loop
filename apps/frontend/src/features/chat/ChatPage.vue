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
        <div v-if="chat.loading" class="chat-transcript q-pa-md" aria-label="Session wird geladen">
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
          :key="chat.activeQueueKey"
          ref="virtualScroll"
          class="chat-transcript q-pa-md"
          :items="displayMessages"
          scroll-target="#chat-scroll-area > .scroll"
          :virtual-scroll-item-size="96"
          @virtual-scroll="handleVirtualScroll"
        >
          <template #before>
            <div v-if="chat.olderMessagesLoading" class="row justify-center q-py-sm" role="status">
              <q-spinner color="primary" size="24px" />
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
            <div :key="message.liveMessageId || message.id" class="chat-transcript__item">
            <div
              :class="[
                'chat-message',
                `chat-message--${message.role}`,
                {
                  'chat-message--turn-continues': message.continuesAssistantTurn,
                  'chat-message--search-target': highlightedMessageId === message.id
                }
              ]"
              :data-message-id="message.id"
              :data-live-message-id="message.liveMessageId || undefined"
              tabindex="-1"
            >
            <span v-if="message.role === 'user'" class="app-sr-only">Benutzernachricht</span>
            <span v-else-if="message.showAgentLabel && !message.workHistory" class="app-sr-only">Assistentenantwort</span>
            <template
              v-if="message.content
                || message.workHistory
                || message.generationStatus === 'interrupted'"
            >
              <AgentActivityPanel
                v-if="message.workHistory"
                hide-provided-files
                :history="message.workHistory"
                :subagents="childrenByMessage.get(message.id) || emptyItems"
                :activity="message.currentTurn ? chat.activity : null"
                :presentation="message.currentTurn ? runPresentation : null"
                :reasoning="message.currentTurn ? chat.streamReasoning : null"
                :context-summary="message.currentTurn ? chat.streamContextSummary : null"
                :warnings="message.currentTurn ? chat.runWarnings : emptyItems"
                :trusted-image-origins="trustedImageOrigins"
                :running="chat.running && message.currentTurn"
                :expanded="expandedWorkHistoryIds.has(message.id)"
                :class="{
                  'q-mb-xs': message.content
                    || message.attachments?.length
                    || message.generationStatus === 'interrupted'
                }"
                @open-file="openToolFile"
                @open-approval-draft="openApprovalDraft"
                @history-expanded="setWorkHistoryExpanded(message.id, $event)"
                @rendered="handleMessageRendered"
                @resize-start="beginActivityResize"
                @resized="finishActivityResize"
                @trust-image-origin="trustImageOrigin"
              >
                <template v-if="message.showAgentLabel" #header>
                  <span class="app-sr-only">Assistentenantwort</span>
                </template>
              </AgentActivityPanel>
              <MessageContent
                v-if="message.preparedReply"
                :content="message.preparedReply.markdown"
                :prepared-replies="message.preparedReply.options"
                class="q-mb-sm"
                @prepared-reply="sendPreparedAnswer"
              />
              <MessageContent
                v-if="message.content && message.role === 'assistant'"
                class="chat-message__content"
                :content="message.content"
                :role="message.error ? 'alert' : undefined"
                :file-actions="message.fileActions"
                :research-sources="message.research?.linkedSources || emptyItems"
                :streaming="message.streaming"
                :trusted-image-origins="trustedImageOrigins"
                @open-file="openToolFile"
                @rendered="handleMessageRendered"
                @trust-image-origin="trustImageOrigin"
              />
              <div v-else-if="message.content" class="chat-message__content">{{ message.commandPrompt || message.content }}</div>
            </template>
            <ChatFiles
              v-if="message.attachments?.length"
              class="q-mt-sm"
              :attachments="message.attachments"
            />
            <FileChanges :actions="message.fileChanges" :git="currentGit" @open-file="openToolFile" @open-diff="openGitDiff" @resize-start="beginActivityResize" @resized="finishActivityResize" />
            <ChatFiles
              :actions="message.providedFiles"
              class="q-my-sm"
              @open-file="openToolFile"
            />
            <div
              v-if="message.generationStatus === 'interrupted' && !message.redirected"
              class="app-muted text-caption row items-center q-gutter-x-xs q-mt-sm"
              role="status"
            >
              <q-icon name="cancel" size="14px" aria-hidden="true" />
              <span>Anfrage gestoppt</span>
            </div>
            <div
              v-if="message.deliveryStatus || message.queueRecoveryId || message.createdAt
                || showMessageModel(message)
                || agentProfileName(message)
                || userMessageSource(message)"
              class="chat-message__meta items-center wrap"
            >
              <q-btn
                v-if="message.role === 'user' && !chat.current?.archived && !String(message.id).startsWith('local-')"
                flat
                round
                dense
                size="sm"
                icon="undo"
                class="chat-message__meta-action chat-message__meta-action--undo"
                :loading="messageActionId === message.id"
                :disable="message.deliveryStatus === 'sending' || (messageActionId !== null && messageActionId !== message.id)"
                aria-label="Nachricht rückgängig machen"
                @click.stop="message.outboxId ? undoQueuedMessage(message.outboxId) : undoMessage(message)"
              >
                <q-tooltip>Nachricht rückgängig machen</q-tooltip>
              </q-btn>
              <template v-if="agentProfileName(message)">
                <span class="chat-message__meta-agent">{{ agentProfileName(message) }}</span>
                <span v-if="showMessageModel(message) || userMessageSource(message) || message.createdAt" aria-hidden="true">·</span>
              </template>
              <span
                v-if="showMessageModel(message)"
                class="chat-message__meta-model"
                :title="message.origin === 'host' ? 'Host' : message.modelName"
              >
                {{ message.origin === 'host' ? 'Host' : message.modelName }}
              </span>
              <span
                v-if="showMessageModel(message) && (userMessageSource(message) || message.createdAt)"
                aria-hidden="true"
              >·</span>
              <template v-if="userMessageSource(message)">
                <span class="chat-message__meta-source">{{ userMessageSource(message) }}</span>
                <span v-if="message.createdAt" aria-hidden="true">·</span>
              </template>
              <time
                v-if="message.createdAt"
                :datetime="message.createdAt"
                :title="formatAbsoluteDateTime(message.createdAt)"
                :aria-label="`Gesendet am ${formatAbsoluteDateTime(message.createdAt)}`"
              >
                {{ formatRelativeDateTime(message.createdAt, messageTimestampNow) }}
              </time>
              <span
                v-if="message.deliveryStatus"
                class="row inline items-center q-gutter-x-xs"
                :class="{ 'text-negative': message.deliveryStatus === 'failed' }"
                role="status"
              >
                <q-icon :name="message.deliveryStatus === 'failed' ? 'error_outline' : 'schedule'" size="1em" aria-hidden="true" />
                <span>{{ message.deliveryStatus === 'sending' ? 'Wird gesendet …' : message.deliveryStatus === 'failed' ? 'Nicht gesendet' : 'Noch nicht gesendet' }}</span>
              </span>
              <span v-if="message.queueRecoveryId" class="text-negative" role="status">Lokaler Versand pausiert</span>
              <q-btn
                v-if="(message.deliveryStatus === 'failed' && message.outboxId === chat.activeQueuedMessages[0]?.id)
                  || (message.queueRecoveryId && message.queueRecoveryId === chat.activeQueuedMessages[0]?.id)"
                flat
                round
                dense
                size="sm"
                icon="refresh"
                color="negative"
                class="chat-message__meta-action"
                :aria-label="message.queueRecoveryId ? 'Versand fortsetzen' : 'Nachricht erneut senden'"
                @click.stop="chat.resumeQueuedSend()"
              />
              <q-btn
                v-if="canRetryMessage(message)"
                flat
                round
                dense
                size="sm"
                icon="refresh"
                color="negative"
                class="chat-message__meta-action chat-message__meta-action--retry"
                :loading="messageActionId === message.id"
                :disable="messageActionId !== null && messageActionId !== message.id"
                aria-label="Anfrage erneut starten"
                @click.stop="retryMessage(message)"
              >
                <q-tooltip>Erneut versuchen</q-tooltip>
              </q-btn>
            </div>
            </div>
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
            <ApprovalCard
              v-for="approval in transcript.unmatchedApprovals"
              :key="approval.id"
              :approval="approval"
              class="q-mt-md"
              @open-approval-draft="openApprovalDraft"
            />
            <QuestionDock
              v-for="question in transcript.unmatchedQuestions"
              :key="question.id"
              :request="question"
              class="q-mt-md"
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
          v-if="isNewConversation"
          :session="chat.activeContext"
          :disabled="draftProjectDisabled"
          class="q-mb-sm"
          @update="chat.updateActive"
        />
        <SessionSettingsFields
          v-if="isNewConversation"
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
          :draft="chat.loading ? '' : chat.activeDraft"
          :disabled="chat.loading || changingDraftProject || !chat.outboxLoaded
            || chat.outboxSaving
            || chat.historyActionPending
            || (!chat.current && chat.activeQueuedMessages.length > 0)"
          :running="!chat.loading && chat.running"
          :stopping="chat.stopping"
          :queue-mode="chat.messageQueueActive"
          :attachments="chat.loading ? emptyItems : chat.activeAttachmentDraft"
          @add-attachments="addAttachmentFiles"
          @remove-attachment="chat.removeAttachmentFile"
          @send="send"
          @stop="chat.stop"
          @update="chat.updateActive"
          @update:draft="chat.setDraft"
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
    <q-dialog
      :transition-duration="0"
      :model-value="Boolean(approvalDraftPreview)"
      maximized
      aria-labelledby="approval-draft-preview-title"
      @update:model-value="approvalDraftPreview = null"
    >
      <q-card v-if="approvalDraftPreview" class="column no-wrap full-height">
        <q-card-section class="row items-center no-wrap q-gutter-sm">
          <div class="col overflow-hidden">
            <h2 id="approval-draft-preview-title" class="text-subtitle1 text-weight-bold q-my-none ellipsis">{{ approvalDraftPreview.filename }}</h2>
            <div class="text-caption app-muted">Skill-Entwurf des Subagenten · Nur Lesezugriff</div>
          </div>
          <q-btn flat round icon="close" aria-label="Entwurfsvorschau schließen" @click="approvalDraftPreview = null" />
        </q-card-section>
        <q-separator />
        <q-scroll-area class="col">
          <div v-if="approvalDraftPreview.loading" class="row justify-center q-pa-lg" role="status">
            <q-spinner color="primary" size="32px" />
            <span class="app-sr-only">Entwurf wird geladen</span>
          </div>
          <q-banner v-else-if="approvalDraftPreview.error" class="app-surface-tonal q-ma-md" role="alert">
            {{ apiErrorMessage(approvalDraftPreview.error) }}
          </q-banner>
          <pre v-else class="file-viewer__text q-pa-md">{{ approvalDraftPreview.content }}</pre>
        </q-scroll-area>
      </q-card>
    </q-dialog>
    <ProjectDialog v-if="draftProjectDialogOpen" v-model="draftProjectDialogOpen" @created="selectCreatedDraftProject" />
  </q-page>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useRoute, useRouter } from 'vue-router'
import ApprovalCard from 'src/features/approvals/ApprovalCard.vue'
import AgentActivityPanel from 'src/features/chat/components/AgentActivityPanel.vue'
import FileChanges from 'src/features/chat/components/FileChanges.vue'
import HomeContent from 'src/features/home/HomeContent.vue'
import ComputerChoiceStrip from 'src/features/chat/components/ComputerChoiceStrip.vue'
import ProjectDialog from 'src/features/projects/ProjectDialog.vue'
import ChatComposer from 'src/features/chat/components/ChatComposer.vue'
import SessionTodoStrip from 'src/features/chat/components/SessionTodoStrip.vue'
import SessionSettingsFields from 'src/features/chat/components/SessionSettingsFields.vue'
import WorkspaceField from 'src/features/coding/WorkspaceField.vue'
import ControlCenterPanel from 'src/app/control-center/ControlCenterPanel.vue'
import MessageContent from 'src/features/chat/components/MessageContent.vue'
import ChatFiles from 'src/features/chat/components/ChatFiles.vue'
import QuestionDock from 'src/features/questions/QuestionDock.vue'
import SubagentInspector from 'src/features/subagents/SubagentInspector.vue'
import SubagentRows from 'src/features/subagents/SubagentRows.vue'
import { useSubagentStore } from 'stores/subagents'
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
import { agentRunPresentation } from 'src/utils/agent-activity'
import {
  chatBottomDistance,
  chatReadingAnchor,
  chatShouldFollowBottom,
  restoreChatReadingAnchor,
  scrollChatToBottom
} from 'src/utils/chat-scroll'
import {
  buildChatTranscript,
  retryAvailableForMessage,
  withLiveTranscriptMessage,
  withQueuedTranscriptMessages
} from 'src/utils/chat-transcript'
import { apiErrorMessage } from 'src/utils/api-errors'
import { notifyError } from 'src/utils/notifications'
import { formatAbsoluteDateTime, formatRelativeDateTime } from 'src/utils/date-time'

defineOptions({ name: 'ChatPage' })

const emptyItems = Object.freeze([])
const route = useRoute()
const router = useRouter()
const $q = useQuasar()
const { confirm } = useConfirm()
const chat = useChatStore()
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
const followingBottom = ref(true)
const messageTimestampNow = ref(Date.now())
const messageActionId = ref(null)
const approvalDraftPreview = ref(null)
const announcedRunStatus = ref('')
const bottomThreshold = 64
const liveWorkHistory = computed(() => ({
  entries: [],
  observedDurationMs: null,
  activeDurationMs: chat.activity.activeDurationMs,
  activeDurationUpdatedAt: chat.activity.activeDurationUpdatedAt
}))
const userMessageSource = (message) => (
  message.role !== 'user' ? ''
    : message.sourceType === 'automationRun' ? `Automation${message.automationName ? ` · ${message.automationName}` : ''}`
    : message.source?.type === 'endpoint' && message.source.provider === 'telegram'
    ? 'Telegram'
    : ''
)
const agentProfileName = (message) => (
  message.role === 'assistant' && !message.continuesAssistantTurn && message.origin !== 'host'
    && !(chat.running && message.currentTurn)
    ? agentProfiles.byId(message.agentProfileId)?.name || 'Allgemein'
    : ''
)
function setWorkHistoryExpanded(messageId, expanded) {
  const next = new Set(expandedWorkHistoryIds.value)
  if (expanded) next.add(messageId)
  else next.delete(messageId)
  expandedWorkHistoryIds.value = next
}
const routeSessionId = computed(() => (
  typeof route.query.session === 'string' ? route.query.session : null
))
const routeSubagentId = computed(() => (
  typeof route.query.subagent === 'string' ? route.query.subagent : null
))
const routeProjectId = computed(() => (
  typeof route.query.project === 'string' ? route.query.project : null
))
const isNewConversation = computed(() => !routeSessionId.value && !chat.current && !chat.loading && !chat.loadError)
const newSessionRequested = computed(() => Object.hasOwn(route.query, 'new'))
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
const routeMessageId = computed(() => (
  typeof route.query.message === 'string' ? route.query.message : null
))
const highlightedMessageId = ref(null)
let resizeObserver = null
let scrollFrame = null
let scrollTarget = null
let lastScrollTop = 0
let userScrollActive = false
let userScrollTimer = null
let messageTimestampTimer = null
let runAnnouncementTimer = null
let touchActive = false
let virtualScrollRefreshFrame = null
let virtualScrollRefreshRequest = null
let virtualScrollRefreshPending = null
let searchHighlightTimer = null
let readingAnchor = null
let virtualScrollRefreshInProgress = false
let virtualScrollRefreshVersion = 0
const sessionApprovals = computed(() => chat.current ? approvals.forSession(chat.current.id) : [])
const sessionQuestions = computed(() => chat.current ? questions.forSession(chat.current.id) : [])
const activeQuestion = computed(() => sessionQuestions.value[0] || null)
const transcript = computed(() => buildChatTranscript(
  chat.current?.messages,
  sessionApprovals.value,
  {
    running: chat.running,
    runId: chat.runId || chat.current?.activeRun?.id,
    toolExecutionActive: chat.activity.phase === 'processing_tool',
    activeToolCallId: chat.activity.toolCallId,
    completedToolCallId: chat.completedToolCallId,
    questions: sessionQuestions.value,
    contextSummaries: chat.current?.contextSummaries
  }
))
const hasCurrentAssistantTurn = computed(() => (
  transcript.value.messages.some((message) => message.currentTurn)
))
const childrenByMessage = computed(() => {
  const result = new Map()
  for (const message of transcript.value.messages) {
    const children = subagents.children.filter((child) => (message.workHistory?.entries || []).some((entry) => (
      entry.tool?.id === child.spawnedByToolCallId && entry.runId === child.spawnedByRunId
    )))
    if (children.length) result.set(message.id, children)
  }
  return result
})
const unmatchedChildren = computed(() => {
  const matched = new Set([...childrenByMessage.value.values()].flat().map((child) => child.id))
  return subagents.children.filter((child) => !matched.has(child.id))
})
const hasCurrentWorkHistory = computed(() => (
  transcript.value.messages.some((message) => message.currentTurn && message.workHistory)
))
const displayMessages = computed(() => {
  const messages = transcript.value.messages
  const liveHistory = chat.running && chat.activity.phase !== 'submitting' && !hasCurrentWorkHistory.value
    ? liveWorkHistory.value
    : null
  const liveMessage = {
    id: `live-${chat.current?.id}`,
    role: 'assistant',
    content: chat.streamContent,
    currentTurn: true,
    modelName: chat.streamModelName,
    research: { linkedSources: chat.streamResearchSources },
    agentProfileId: chat.runAgentProfileId || chat.current?.activeRun?.agentProfileId || null,
    showAgentLabel: !hasCurrentAssistantTurn.value,
    streaming: Boolean(chat.streamContent),
    workHistory: liveHistory
  }
  return withQueuedTranscriptMessages(
    withLiveTranscriptMessage(messages, liveMessage), chat.activeQueuedMessages, chat.queueSendingEntryId
  )
})
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

async function openApprovalDraft(approval) {
  const draft = approval?.details?.draftFile
  const sessionId = approval?.sessionId
  if (!['build_skill', 'edit_skill'].includes(approval?.actionKind) || !draft || chat.current?.id !== sessionId) return
  if (approval.payload?.subagentSessionId) {
    approvalDraftPreview.value = { filename: draft.filename, loading: true, content: '', error: null }
    const preview = approvalDraftPreview.value
    try {
      const data = await approvals.previewSkillDraft(approval)
      if (approvalDraftPreview.value !== preview) return
      if (data?.kind !== 'text' || typeof data.content !== 'string' || !draft.fileRevision) {
        preview.error = { code: 'skill.draftInvalid' }
      } else if (data.revision !== draft.fileRevision) {
        preview.error = { code: 'file.changed' }
      } else {
        preview.content = data.content
      }
    } catch (error) {
      if (approvalDraftPreview.value === preview) preview.error = error
    } finally {
      if (approvalDraftPreview.value === preview) preview.loading = false
    }
    return
  }
  const tabId = await controlCenter.openFile({
    scope: 'session',
    path: draft.path,
    name: draft.filename,
    rootIdentity: draft.rootIdentity || sessionId,
    readOnly: false
  })
  if (tabId) workspace.startFileTabEdit(tabId)
}

function trustImageOrigin(origin) {
  const sessionId = chat.current?.id
  if (!sessionId || trustedImageOrigins.value.includes(origin)) return
  const next = new Map(trustedImageOriginsBySession.value)
  next.set(sessionId, [...trustedImageOrigins.value, origin])
  trustedImageOriginsBySession.value = next
}

function showMessageModel(message) {
  if (message.origin === 'host') return true
  return message.role === 'assistant'
    && Boolean(message.hasAssistantContent || message.content)
    && Boolean(message.modelName)
}

function canRetryMessage(message) {
  return !chat.current?.archived && retryAvailableForMessage(message, chat.current?.messages, chat.running)
}

async function undoMessage(message) {
  if (composer.value?.hasAudioActivity?.()) {
    $q.notify({
      type: 'info',
      message: 'Beende oder brich zuerst die laufende Aufnahme beziehungsweise Transkription ab.'
    })
    return
  }
  const runningHint = chat.running ? ' Die laufende Anfrage wird dabei abgebrochen.' : ''
  const accepted = await confirm(
    `Diese Nachricht und alle späteren Chatnachrichten werden entfernt.${runningHint} `
      + 'Der aktuelle Entwurf, seine Anhänge und ein vorhandenes Diktat werden ersetzt. '
      + 'Bereits ausgeführte Tool-, Datei-, Berechtigungs- und Memory-Aktionen bleiben bestehen.',
    'Nachricht rückgängig machen?',
    { okLabel: 'Rückgängig machen' }
  )
  if (!accepted) return
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
  if (composer.value?.hasAudioActivity?.()) {
    $q.notify({
      type: 'info',
      message: 'Beende oder brich zuerst die laufende Aufnahme beziehungsweise Transkription ab.'
    })
    return
  }
  const hasDraft = Boolean(
    chat.activeDraft.trim()
    || chat.activeAttachmentDraft.length
    || chat.getAudioDictation(chat.current?.id || null)
  )
  if (hasDraft && !await confirm(
    'Der aktuelle Entwurf, seine Anhänge und ein vorhandenes Diktat werden ersetzt.',
    'Vorgemerkte Nachricht rückgängig machen?',
    { okLabel: 'Rückgängig machen' }
  )) return
  const restored = await chat.restoreQueuedMessage(id)
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
    scheduleFollowScroll()
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

async function loadRouteSession() {
  const sessionId = routeSessionId.value
  await composer.value?.prepareAudioNavigation?.()
  if (routeSessionId.value !== sessionId) return
  const session = await chat.loadSession(sessionId).catch(() => null)
  if (routeSessionId.value !== sessionId) return
  if (session?.readOnly && session.parentSessionId) {
    await router.replace({ name: 'chat', query: { session: session.parentSessionId, subagent: session.id } })
    return
  }
  if (chat.selectedSessionId !== sessionId || chat.loadError) return
  await reconcileRouteProject()
  const requestedApp = typeof route.query.controlCenter === 'string'
    ? route.query.controlCenter
    : null
  if (requestedApp) {
    try {
      await controlCenter.openApp(requestedApp)
    } catch {
      // The central API interceptor displays the error and restores the previous tabs.
    }
  }
  await nextTick()
  if (routeMessageId.value) await revealRouteMessage()
  else if (!showHome.value) composer.value?.focus()
}

async function reconcileRouteProject() {
  if (
    !chat.current
    || chat.current.id !== routeSessionId.value
    || (chat.current.projectId || null) === routeProjectId.value
  ) return
  await router.replace({
    name: 'chat',
    query: {
      ...(chat.current.projectId ? { project: chat.current.projectId } : {}),
      session: chat.current.id,
      ...(routeSubagentId.value ? { subagent: routeSubagentId.value } : {}),
      ...(typeof route.query.controlCenter === 'string'
        ? { controlCenter: route.query.controlCenter }
        : {}),
      ...(routeMessageId.value ? { message: routeMessageId.value } : {}),
      ...(typeof route.query.automation === 'string' ? { automation: route.query.automation } : {}),
      ...(typeof route.query.run === 'string' ? { run: route.query.run } : {})
    }
  })
}

async function revealRouteMessage() {
  const messageId = routeMessageId.value
  if (!messageId || chat.current?.id !== routeSessionId.value) return
  let index = displayMessages.value.findIndex((message) => message.id === messageId)
  if (index < 0 && chat.current.nextMessageCursor) {
    try {
      await chat.loadThroughMessage(messageId)
    } catch {
      return
    }
    await nextTick()
    index = displayMessages.value.findIndex((message) => message.id === messageId)
  }
  if (index < 0) return
  followingBottom.value = false
  cancelFollowScroll()
  highlightedMessageId.value = messageId
  await nextTick()
  virtualScroll.value?.scrollTo(index, 'center-force')
  await new Promise((resolve) => window.requestAnimationFrame(resolve))
  const target = findMessageElement(messageId)
  // Align the rendered message after virtual scrolling; expanded history changes item heights.
  if (target && scrollTarget) {
    restoreChatReadingAnchor(scrollTarget, target, {
      offsetTop: Math.max(0, (scrollTarget.clientHeight - target.getBoundingClientRect().height) / 2)
    })
  }
  target?.focus({ preventScroll: true })
  rememberReadingAnchor()
  clearTimeout(searchHighlightTimer)
  searchHighlightTimer = window.setTimeout(() => {
    if (highlightedMessageId.value === messageId) highlightedMessageId.value = null
  }, 2500)
}

async function loadOlderHistory() {
  if (!chat.current?.nextMessageCursor || chat.olderMessagesLoading) return
  try {
    await chat.loadOlderMessages()
  } catch {
    // The store exposes the retry state above the transcript.
  }
}

function handleVirtualScroll({ from, direction }) {
  if (virtualScrollRefreshInProgress) {
    void completeVirtualScrollRefresh()
    return
  }
  if (!followingBottom.value) rememberReadingAnchor()
  if (
    userScrollActive
    && direction === 'decrease'
    && from <= 3
    && !followingBottom.value
    && chat.current?.nextMessageCursor
  ) void loadOlderHistory()
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
  const sessionId = chat.current?.id
  enableFollowMode()
  const request = chat.send(content, options)
  try {
    const result = await request
    if (!result || !sessionId || chat.current?.id !== sessionId) return
    await nextTick()
    scheduleFollowScroll()
  } finally {
    await nextTick()
    composer.value?.focus()
  }
}

async function sendPreparedAnswer(content) {
  enableFollowMode()
  await chat.send(content, { preserveDraft: true })
  await nextTick()
  scheduleFollowScroll()
}

async function flushQueuedMessage() {
  if (!chat.queuedSendReady) return
  const request = chat.drainOutbox()
  await nextTick()
  scheduleFollowScroll()
  await request
}

function addAttachmentFiles(files, done, options) {
  const result = chat.addAttachmentFiles(files, options)
  if (result.error) $q.notify({ type: 'negative', message: result.error })
  done?.(result)
}

function scheduleVirtualScrollRefresh(preservePosition = true, resetSizes = false) {
  if (!virtualScroll.value || displayMessages.value.length === 0) return
  const request = {
    preservePosition,
    anchor: preservePosition && !followingBottom.value
      ? readingAnchor || captureReadingAnchor()
      : null,
    revealing: false,
    resetSizes: resetSizes || virtualScrollRefreshPending?.resetSizes,
    version: ++virtualScrollRefreshVersion
  }
  if (userScrollActive || virtualScrollRefreshInProgress) {
    virtualScrollRefreshPending = request
    return
  }
  startVirtualScrollRefresh(request)
}

function startVirtualScrollRefresh(request) {
  virtualScrollRefreshRequest = request
  virtualScrollRefreshInProgress = true
  virtualScrollRefreshFrame = window.requestAnimationFrame(() => {
    virtualScrollRefreshFrame = null
    if (virtualScrollRefreshRequest !== request) return
    if (userScrollActive) {
      virtualScrollRefreshPending = {
        ...(virtualScrollRefreshPending || request),
        resetSizes: request.resetSizes || virtualScrollRefreshPending?.resetSizes
      }
      finishVirtualScrollRefresh(request)
      return
    }
    if (request.resetSizes) virtualScroll.value?.reset()
    else virtualScroll.value?.refresh(-1)
  })
}

async function completeVirtualScrollRefresh() {
  const request = virtualScrollRefreshRequest
  if (!request) return
  await nextTick()
  await new Promise((resolve) => window.requestAnimationFrame(resolve))
  if (request !== virtualScrollRefreshRequest) return
  if (
    request.anchor
    && !userScrollActive
    && !followingBottom.value
    && request.version === virtualScrollRefreshVersion
  ) {
    const element = findMessageElement(request.anchor.id)
    if (!element && !request.revealing) {
      const index = displayMessages.value.findIndex((message) => message.id === request.anchor.id)
      if (index >= 0) {
        request.revealing = true
        virtualScroll.value?.scrollTo(index, 'start-force')
        return
      }
    }
    restoreChatReadingAnchor(scrollTarget, element, request.anchor)
    readingAnchor = {
      ...request.anchor,
      id: element?.dataset.messageId || request.anchor.id
    }
  }
  finishVirtualScrollRefresh(request)
}

function finishVirtualScrollRefresh(request) {
  if (request !== virtualScrollRefreshRequest) return
  virtualScrollRefreshRequest = null
  virtualScrollRefreshInProgress = false
  resumePendingVirtualScrollRefresh()
}

function resumePendingVirtualScrollRefresh() {
  if (userScrollActive || !virtualScrollRefreshPending) return
  const pending = virtualScrollRefreshPending
  virtualScrollRefreshPending = null
  // A deferred layout update must preserve where the gesture ended, not where it began.
  pending.anchor = pending.preservePosition && !followingBottom.value
    ? captureReadingAnchor()
    : null
  pending.version = ++virtualScrollRefreshVersion
  startVirtualScrollRefresh(pending)
}

function handleMessageRendered() {
  if (followingBottom.value) {
    scheduleVirtualScrollRefresh(false)
    scheduleFollowScroll()
    return
  }
  scheduleVirtualScrollRefresh()
}

function renderedMessageElements() {
  return [...(scrollContent.value?.querySelectorAll('[data-message-id]') || [])]
}

function findMessageElement(messageId) {
  return renderedMessageElements().find((element) => (
    element.dataset.messageId === messageId || element.dataset.liveMessageId === messageId
  ))
}

function captureReadingAnchor() {
  return chatReadingAnchor(scrollTarget, renderedMessageElements())
}

function rememberReadingAnchor() {
  if (followingBottom.value || virtualScrollRefreshInProgress) return
  readingAnchor = captureReadingAnchor() || readingAnchor
}

function clearReadingAnchor() {
  readingAnchor = null
  virtualScrollRefreshVersion += 1
  if (virtualScrollRefreshRequest) virtualScrollRefreshRequest.anchor = null
  if (virtualScrollRefreshPending) virtualScrollRefreshPending.anchor = null
}

function beginActivityResize() {
  if (!chat.current) return
  followingBottom.value = false
  clearReadingAnchor()
  cancelFollowScroll()
}

function finishActivityResize() {
  scheduleVirtualScrollRefresh(false)
}

function updateFollowState() {
  if (!scrollTarget) return
  const scrollTop = scrollTarget.scrollTop
  const movedUp = scrollTop < lastScrollTop
  lastScrollTop = scrollTop
  if (virtualScrollRefreshInProgress && !userScrollActive) return
  const atBottom = chatBottomDistance(scrollTarget) <= bottomThreshold
  const shouldFollow = chatShouldFollowBottom(
    followingBottom.value,
    userScrollActive && movedUp,
    userScrollActive && atBottom
  )
  if (shouldFollow && !followingBottom.value) {
    followingBottom.value = true
    clearReadingAnchor()
  }
  else if (!shouldFollow && followingBottom.value) {
    followingBottom.value = false
    cancelFollowScroll()
  }
  if (!followingBottom.value) rememberReadingAnchor()
  if (userScrollActive && !touchActive) scheduleUserScrollEnd()
}

function cancelFollowScroll() {
  if (scrollFrame === null) return
  window.cancelAnimationFrame(scrollFrame)
  scrollFrame = null
}

function beginUserScroll() {
  if (virtualScrollRefreshInProgress) clearReadingAnchor()
  userScrollActive = true
  clearTimeout(userScrollTimer)
  userScrollTimer = null
  cancelFollowScroll()
}

function scheduleUserScrollEnd() {
  clearTimeout(userScrollTimer)
  userScrollTimer = setTimeout(() => {
    userScrollTimer = null
    userScrollActive = false
    if (!virtualScrollRefreshInProgress) resumePendingVirtualScrollRefresh()
    scheduleFollowScroll()
  }, 180)
}

function handleTouchStart() {
  touchActive = true
  beginUserScroll()
}

function handleTouchMove() {
  if (!userScrollActive) beginUserScroll()
}

function handleTouchEnd() {
  touchActive = false
  if (userScrollActive) scheduleUserScrollEnd()
}

function handleWheel(event) {
  if (!event.deltaY) return
  beginUserScroll()
  scheduleUserScrollEnd()
}

function handleScrollKey(event) {
  if (
    event.defaultPrevented
    || event.altKey
    || event.ctrlKey
    || event.metaKey
    || !['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)
  ) return
  beginUserScroll()
  scheduleUserScrollEnd()
}

function handleScrollAreaMouseDown(event) {
  if (event.button !== 0 || !scrollTarget || scrollTarget.contains(event.target)) return
  beginUserScroll()
  window.removeEventListener('mouseup', handleScrollAreaMouseUp)
  window.addEventListener('mouseup', handleScrollAreaMouseUp, { once: true })
}

function handleScrollAreaMouseUp() {
  scheduleUserScrollEnd()
}

function enableFollowMode() {
  touchActive = false
  userScrollActive = false
  clearTimeout(userScrollTimer)
  userScrollTimer = null
  followingBottom.value = true
  clearReadingAnchor()
  cancelFollowScroll()
  scheduleBottomScroll()
}

function followScrollNow() {
  if (followingBottom.value && !userScrollActive && scrollTarget) {
    scrollChatToBottom(scrollTarget)
  }
}

function scheduleFollowScroll() {
  if (!followingBottom.value) return
  scheduleBottomScroll()
}

function scheduleBottomScroll() {
  if (showHome.value || userScrollActive || scrollFrame !== null) return
  scrollFrame = window.requestAnimationFrame(() => {
    scrollFrame = null
    if (scrollTarget) scrollChatToBottom(scrollTarget)
  })
}

function observeChatSize() {
  scrollTarget = scrollArea.value?.getScrollTarget?.() || null
  if (scrollTarget) {
    lastScrollTop = scrollTarget.scrollTop
    scrollTarget.addEventListener('scroll', updateFollowState, { passive: true })
    scrollTarget.addEventListener('touchstart', handleTouchStart, { passive: true })
    scrollTarget.addEventListener('touchmove', handleTouchMove, { passive: true })
    scrollTarget.addEventListener('touchend', handleTouchEnd, { passive: true })
    scrollTarget.addEventListener('touchcancel', handleTouchEnd, { passive: true })
    scrollTarget.addEventListener('wheel', handleWheel, { passive: true })
  }
  if (typeof ResizeObserver === 'undefined') return
  resizeObserver = new ResizeObserver(() => scheduleFollowScroll())
  if (scrollContent.value) resizeObserver.observe(scrollContent.value)
  if (scrollTarget) resizeObserver.observe(scrollTarget)
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
  cancelFollowScroll()
  await nextTick()
  if (scrollTarget) scrollTarget.scrollTop = 0
})
watch(routeSessionId, (id) => {
  approvalDraftPreview.value = null
  void subagents.loadChildren(id)
  void loadRouteSession()
}, { immediate: true })
watch(routeProjectId, () => void reconcileRouteProject())
watch(routeMessageId, () => void revealRouteMessage())
watch(() => chat.activeContext.sessionProfile?.messageSize || 'standard', () => {
  scheduleVirtualScrollRefresh(true, true)
  scheduleFollowScroll()
}, { flush: 'pre' })
watch(
  [() => transcript.value.messages, () => chat.activeQueuedMessages.map((entry) => [entry.id, entry.paused, entry.sentMessageId])],
  () => {
    if (followingBottom.value) scheduleFollowScroll()
    else scheduleVirtualScrollRefresh()
  },
  { flush: 'pre' }
)
watch(
  () => Boolean(chat.streamContent),
  (streaming) => {
    if (!followingBottom.value || userScrollActive) return
    cancelFollowScroll()
    if (streaming) {
      followScrollNow()
      return
    }
    scheduleFollowScroll()
  },
  { flush: 'post' }
)
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
  () => chat.current?.id,
  async (sessionId, previousSessionId) => {
    if (!sessionId || sessionId === previousSessionId) return
    followingBottom.value = true
    clearReadingAnchor()
    cancelFollowScroll()
    if (routeMessageId.value) {
      followingBottom.value = false
      return
    }
    await nextTick()
    scheduleFollowScroll()
  }
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
    if (ready) void flushQueuedMessage()
  },
  { immediate: true }
)
watch(
  () => chat.createdSessionResult,
  async (result) => {
    if (!result) return
    chat.createdSessionResult = null
    if (routeProjectId.value) void projects.fetchAll().catch(() => {})
    await router.replace({
      name: 'chat',
      query: {
        ...(routeProjectId.value ? { project: routeProjectId.value } : {}),
        session: result.sessionId,
        ...(result.appId ? { controlCenter: result.appId } : {}),
        ...(result.appId === 'automation' ? { automation: 'new' } : {})
      }
    })
  }
)
onMounted(() => {
  observeChatSize()
  messageTimestampTimer = window.setInterval(() => {
    messageTimestampNow.value = Date.now()
  }, 60_000)
  void loadBackgroundData()
  executionAgents.startPolling()
})
onBeforeUnmount(() => {
  draftProjectCreationContext = null
  approvalDraftPreview.value = null
  void subagents.loadChildren(null)
  virtualScrollRefreshVersion += 1
  resizeObserver?.disconnect()
  scrollTarget?.removeEventListener('scroll', updateFollowState)
  scrollTarget?.removeEventListener('touchstart', handleTouchStart)
  scrollTarget?.removeEventListener('touchmove', handleTouchMove)
  scrollTarget?.removeEventListener('touchend', handleTouchEnd)
  scrollTarget?.removeEventListener('touchcancel', handleTouchEnd)
  scrollTarget?.removeEventListener('wheel', handleWheel)
  window.clearInterval(messageTimestampTimer)
  executionAgents.stopPolling()
  clearTimeout(runAnnouncementTimer)
  clearTimeout(userScrollTimer)
  clearTimeout(searchHighlightTimer)
  window.removeEventListener('mouseup', handleScrollAreaMouseUp)
  if (virtualScrollRefreshFrame !== null) window.cancelAnimationFrame(virtualScrollRefreshFrame)
  cancelFollowScroll()
})
</script>
