import { defineStore } from 'pinia'
import { toRaw } from 'vue'
import { useApprovalStore } from 'src/stores/approvals'
import { useQuestionStore } from 'src/stores/questions'
import { releaseAttachmentDrafts } from 'src/features/chat/attachments'
import { defaultSessionOptions, emptyActivity, emptyDrafts, emptySessionList, newSessionQueueKey, normalizedNewSessionDraft, queueKeyForEntry, sessionScopeKey, sortSessions } from 'src/features/chat/state'
import { compareChatOutboxEntries, deleteChatOutboxEntries, saveChatOutboxEntries } from 'src/utils/chat-outbox'
import { notifyError } from 'src/utils/notifications'
import { draftActions } from 'src/features/chat/draft'
import { sessionsActions } from 'src/features/chat/sessions'
import { outboxActions } from 'src/features/chat/outbox'
import { streamActions } from 'src/features/chat/stream'

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessionLists: { standalone: emptySessionList() },
    sessionCatalog: [],
    sessionCatalogVersion: 0,
    sessionsProjectId: null,
    current: null,
    loading: false,
    running: false,
    stopping: false,
    cancelRequested: false,
    streamContent: '',
    streamResearchSources: [],
    streamReasoning: null,
    streamContextSummary: null,
    runWarnings: [],
    streamModelName: null,
    processOutputs: {},
    completedToolCallId: null,
    eventSource: null,
    selectedSessionId: null,
    runSessionId: null,
    runId: null,
    runAgentProfileId: null,
    reconnectTimer: null,
    reconnectAttempts: 0,
    activity: emptyActivity(),
    sessionLoadVersion: 0,
    sessionLoadPromise: null,
    historyActionPending: false,
    sessionSettingsPending: 0,
    sessionSettingsPromise: null,
    sessionRequestController: null,
    sessionRefreshPromise: null,
    sessionRefreshQueued: null,
    olderMessagesLoading: false,
    olderMessagesLoadFailed: false,
    olderMessagesPromise: null,
    loadError: null,
    sessionDefaults: defaultSessionOptions(),
    draftPreviewRequests: {},
    draftOwnerUserId: null,
    drafts: emptyDrafts(),
    attachmentDrafts: {
      newSession: [],
      newSessions: { standalone: [] },
      sessionMessages: {}
    },
    audioDictations: {
      newSession: null,
      newSessions: { standalone: null },
      sessionMessages: {}
    },
    queuedMessages: {},
    queuedSendPausedSessions: {},
    outboxLoaded: false,
    outboxLoadFailed: false,
    outboxSaving: false,
    outboxLoadVersion: 0,
    outboxLoadPromise: null,
    queueDrainPromise: null,
    queueDrainTimer: null,
    queueSendingEntryId: null,
    emptySessionPromise: null,
    createdSessionResult: null
  }),
  getters: {
    activeSessionList: (state) => state.sessionLists[sessionScopeKey(state.sessionsProjectId)]
      || emptySessionList(),
    sessions() {
      return this.activeSessionList.items
    },
    cachedSessions: (state) => {
      const sessions = new Map(state.sessionCatalog.map((session) => [session.id, session]))
      for (const list of Object.values(state.sessionLists)) {
        for (const session of list.items) sessions.set(session.id, session)
      }
      return [...sessions.values()]
    },
    sessionList: (state) => (projectId = null) => (
      state.sessionLists[sessionScopeKey(projectId)] || null
    ),
    activeContext: (state) => state.current || state.drafts.newSession.options,
    activeAgentProfileId: (state) => state.current
      ? state.drafts.sessionAgentProfiles[state.current.id] || state.current.agentProfileId
      : state.drafts.newSession.options.agentProfileId,
    activeDraft: (state) => state.current
      ? state.drafts.sessionMessages[state.current.id] || ''
      : state.drafts.newSession.message,
    activeAttachmentDraft: (state) => state.current
      ? state.attachmentDrafts.sessionMessages[state.current.id] || []
      : state.attachmentDrafts.newSession,
    activeQueueKey: (state) => state.current?.id || newSessionQueueKey(state.sessionsProjectId),
    activeQueuedMessages() {
      return this.queuedMessages[this.activeQueueKey] || []
    },
    queuedSendPaused() {
      return Boolean(this.queuedSendPausedSessions[this.activeQueueKey])
    },
    messageQueueActive() {
      if (this.activeQueuedMessages.length) return true
      if (!this.current) return false
      if (this.running || this.stopping || this.runId) return true
      const sessionId = this.current.id
      return Boolean(
        useApprovalStore().forSession(sessionId).length
        || useQuestionStore().forSession(sessionId).length
      )
    },
    queuedSendReady() {
      if (!this.outboxLoaded || this.queuedSendPaused) return false
      if (this.loading || this.sessionRequestController || this.stopping) return false
      if (this.historyActionPending || this.sessionSettingsPending > 0) return false
      if (!this.activeQueuedMessages.length) return false
      if (this.running || this.runId) {
        return Boolean(this.current && this.runId
          && this.runSessionId === this.current.id
          && this.activeQueuedMessages[0].continuationRunId === this.runId)
      }
      const sessionId = this.current?.id
      if (!sessionId) return this.selectedSessionId === null
      if (useApprovalStore().forSession(sessionId).length) return false
      if (useQuestionStore().forSession(sessionId).length) return false
      return true
    }
  },
  actions: {
    ensureSessionList(projectId = null) {
      const scope = sessionScopeKey(projectId)
      if (!this.sessionLists[scope]) this.sessionLists[scope] = emptySessionList()
      return this.sessionLists[scope]
    },
    findCachedSession(id) {
      return this.cachedSessions.find((session) => session.id === id) || null
    },
    updateCachedSession(id, update) {
      for (const list of Object.values(this.sessionLists)) {
        const index = list.items.findIndex((session) => session.id === id)
        if (index < 0) continue
        list.version += 1
        list.items[index] = update(list.items[index])
        list.items = sortSessions(list.items)
      }
      const catalogIndex = this.sessionCatalog.findIndex((session) => session.id === id)
      if (catalogIndex >= 0) {
        this.sessionCatalogVersion += 1
        this.sessionCatalog[catalogIndex] = update(this.sessionCatalog[catalogIndex])
        this.sessionCatalog = sortSessions(this.sessionCatalog)
      }
    },
    replaceAgentProfile(profileId, replacementId) {
      for (const session of this.cachedSessions) {
        if (session.agentProfileId === profileId) {
          this.updateCachedSession(session.id, (item) => ({ ...item, agentProfileId: replacementId }))
        }
      }
      if (this.current?.agentProfileId === profileId) {
        this.current.agentProfileId = replacementId
      }
    },
    removeCachedSession(id) {
      for (const list of Object.values(this.sessionLists)) {
        if (!list.items.some((session) => session.id === id)) continue
        list.version += 1
        list.items = list.items.filter((session) => session.id !== id)
      }
      if (this.sessionCatalog.some((session) => session.id === id)) this.sessionCatalogVersion += 1
      this.sessionCatalog = this.sessionCatalog.filter((session) => session.id !== id)
    },
    async prepareProjectOutboxDeletion(projectId, sessionIds = []) {
      if (this.outboxLoadPromise) await this.outboxLoadPromise
      if (!this.outboxLoaded || this.outboxLoadFailed) {
        notifyError({}, 'Vorgemerkte Nachrichten konnten nicht geladen werden.')
        throw new Error('Chat outbox is unavailable')
      }
      this.cancelQueueDrain()
      if (this.queueDrainPromise) await this.queueDrainPromise
      this.cancelQueueDrain()
      const ids = new Set(sessionIds)
      const entries = Object.values(this.queuedMessages).flat().filter((entry) => (
        entry.projectId === projectId || ids.has(entry.sessionId)
      ))
      const snapshot = {
        entries: [...entries],
        ownerUserId: this.draftOwnerUserId,
        loadVersion: this.outboxLoadVersion
      }
      if (!entries.length) return snapshot
      try {
        await deleteChatOutboxEntries(entries.map((entry) => entry.id))
      } catch (error) {
        this.scheduleQueueDrain(0)
        notifyError({}, 'Vorgemerkte Nachrichten konnten nicht gelöscht werden.')
        throw error
      }
      const entryIds = new Set(entries.map((entry) => entry.id))
      for (const [key, queue] of Object.entries(this.queuedMessages)) {
        const remaining = queue.filter((entry) => !entryIds.has(entry.id))
        if (remaining.length) this.queuedMessages[key] = remaining
        else delete this.queuedMessages[key]
        if (remaining[0]?.paused) this.queuedSendPausedSessions[key] = true
        else delete this.queuedSendPausedSessions[key]
      }
      return snapshot
    },
    async restoreProjectOutboxDeletion(snapshot) {
      if (
        snapshot?.entries.length
        && snapshot.ownerUserId === this.draftOwnerUserId
        && snapshot.loadVersion === this.outboxLoadVersion
      ) {
        try {
          await saveChatOutboxEntries(snapshot.entries.map((entry) => toRaw(entry)))
        } catch {
          notifyError({}, 'Vorgemerkte Nachrichten konnten nicht wiederhergestellt werden.')
        }
        for (const entry of snapshot.entries) {
          const key = queueKeyForEntry(entry)
          const queue = this.queuedMessages[key] || []
          if (!queue.some((item) => item.id === entry.id)) queue.push(entry)
          queue.sort(compareChatOutboxEntries)
          this.queuedMessages[key] = queue
          if (queue[0]?.paused) this.queuedSendPausedSessions[key] = true
          else delete this.queuedSendPausedSessions[key]
        }
      }
      this.scheduleQueueDrain(0)
    },
    async removeProjectScope(projectId, projectSessionIds = [], outboxDeletion = null) {
      if (this.outboxLoadPromise) await this.outboxLoadPromise
      const scope = sessionScopeKey(projectId)
      this.sessionCatalogVersion += 1
      const sessionIds = new Set(projectSessionIds)
      for (const session of this.cachedSessions) {
        if (session.projectId === projectId) sessionIds.add(session.id)
      }
      if (this.current?.projectId === projectId) sessionIds.add(this.current.id)
      for (const entries of Object.values(this.queuedMessages)) {
        for (const entry of entries) {
          if (entry.projectId === projectId && entry.sessionId) sessionIds.add(entry.sessionId)
        }
      }
      if (this.sessionsProjectId === projectId) {
        if (this.selectedSessionId) sessionIds.add(this.selectedSessionId)
        if (this.runSessionId) sessionIds.add(this.runSessionId)
      }

      this.cancelQueueDrain()
      const queueKeys = Object.entries(this.queuedMessages)
        .filter(([key, entries]) => (
          key === newSessionQueueKey(projectId)
          || entries.some((entry) => entry.projectId === projectId || sessionIds.has(entry.sessionId))
        ))
        .map(([key]) => key)
      for (const key of queueKeys) {
        releaseAttachmentDrafts((this.queuedMessages[key] || []).flatMap((entry) => entry.attachments))
        delete this.queuedMessages[key]
        delete this.queuedSendPausedSessions[key]
      }

      const activeSessionAffected = sessionIds.has(this.current?.id)
        || sessionIds.has(this.selectedSessionId)
        || sessionIds.has(this.runSessionId)
      if (activeSessionAffected) await this.loadSession(null)
      for (const id of sessionIds) {
        this.removeCachedSession(id)
        delete this.drafts.sessionMessages[id]
        delete this.drafts.sessionAgentProfiles[id]
        this.clearAttachmentDraft(id)
        this.setAudioDictation(null, id)
      }
      for (const attachment of this.attachmentDrafts.newSessions[scope] || []) {
        if (attachment.objectUrl) globalThis.URL?.revokeObjectURL?.(attachment.objectUrl)
      }
      delete this.sessionLists[scope]
      delete this.drafts.newSessions[scope]
      delete this.attachmentDrafts.newSessions[scope]
      delete this.audioDictations.newSessions[scope]
      if (this.sessionsProjectId === projectId) {
        this.sessionsProjectId = null
        this.ensureSessionList(null)
        if (!this.drafts.newSessions.standalone) {
          this.drafts.newSessions.standalone = normalizedNewSessionDraft(null)
        }
        if (!this.attachmentDrafts.newSessions.standalone) {
          this.attachmentDrafts.newSessions.standalone = []
        }
        if (!Object.hasOwn(this.audioDictations.newSessions, 'standalone')) {
          this.audioDictations.newSessions.standalone = null
        }
        this.drafts.newSession = this.drafts.newSessions.standalone
        this.attachmentDrafts.newSession = this.attachmentDrafts.newSessions.standalone
        this.audioDictations.newSession = this.audioDictations.newSessions.standalone
      }
      this.persistDrafts()
      releaseAttachmentDrafts((outboxDeletion?.entries || []).flatMap((entry) => entry.attachments))
      if (Object.values(this.queuedMessages).some((queue) => queue.length)) {
        this.scheduleQueueDrain(0)
      }
    },
    ...draftActions,
    ...sessionsActions,
    ...outboxActions,
    ...streamActions
  }
})

export {
  buildAttachmentFormData,
  downloadMessageAttachmentFiles,
  MAX_ATTACHMENT_BYTES,
  MAX_ATTACHMENT_DRAFT_BYTES,
  MAX_ATTACHMENT_DRAFTS
} from 'src/features/chat/attachments'
