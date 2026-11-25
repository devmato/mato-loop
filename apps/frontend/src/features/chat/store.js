import { defineStore } from 'pinia'
import { toRaw } from 'vue'
import { api } from 'src/boot/axios'
import { useApprovalStore } from 'src/stores/approvals'
import { useQuestionStore } from 'src/stores/questions'
import { releaseAttachmentDrafts } from 'src/features/chat/attachments'
import { compareSessions, defaultSessionOptions, emptyActivity, emptyDrafts, emptySessionList, mergeSessionSummary, newSessionQueueKey, normalizedNewSessionDraft, queueKeyForEntry, sessionScopeKey, sortSessions } from 'src/features/chat/state'
import { compareChatOutboxEntries, deleteChatOutboxEntries, saveChatOutboxEntries } from 'src/features/chat/outbox-storage'
import { notifyError } from 'src/utils/notifications'
import { draftActions } from 'src/features/chat/draft'
import { sessionsActions } from 'src/features/chat/sessions'
import { outboxActions } from 'src/features/chat/outbox'
import { streamActions } from 'src/features/chat/stream'
import { useSessionTabsStore } from './session-tabs-store'

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessionLists: { standalone: emptySessionList() },
    visibleSessionScopes: ['standalone'],
    sessionCatalog: [],
    sessionCatalogVersion: 0,
    sessionsProjectId: null,
    current: null,
    liveTranscriptSessionId: null,
    personalChatSessionId: null,
    sessionDetails: [],
    loading: false,
    running: false,
    stopping: false,
    cancelRequested: false,
    streamContent: '',
    streamUserMessageId: null,
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
    profileSelectionVersions: {},
    sessionRequestController: null,
    sessionRefreshPromise: null,
    sessionRefreshQueued: null,
    messageRefreshRequests: {},
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
    pendingOutboxEntry: null,
    pendingOutboxSave: null,
    outboxLoadVersion: 0,
    outboxLoadPromise: null,
    serverQueueVersion: 0,
    queueDrainPromise: null,
    queueDrainTimer: null,
    queueSendingEntryId: null,
    queueUndoId: null,
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
    sessionHasPendingSend: (state) => (sessionId, projectId = null) => {
      const key = sessionId || newSessionQueueKey(projectId)
      const pending = state.pendingOutboxEntry
      const first = state.queuedMessages[key]?.[0]
      return Boolean((pending && queueKeyForEntry(pending) === key) || (first && !first.paused))
    },
    activePendingMessage() {
      return this.pendingOutboxEntry && queueKeyForEntry(this.pendingOutboxEntry) === this.activeQueueKey
        ? this.pendingOutboxEntry : null
    },
    activeQueuedMessages() {
      const queue = this.queuedMessages[this.activeQueueKey] || []
      return this.activePendingMessage
        ? [...queue, this.activePendingMessage].sort(compareChatOutboxEntries) : queue
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
      if (!this.outboxLoaded) return false
      if (this.loading || this.sessionRequestController || this.stopping) return false
      if (this.historyActionPending || this.queueUndoId || this.sessionSettingsPending > 0) return false
      const entry = this.activeQueuedMessages.find((item) => !item.serverQueued)
      if (!entry || entry.paused || entry.id === this.pendingOutboxEntry?.id) return false
      const sessionId = this.current?.id
      if (!sessionId) return this.selectedSessionId === null
      return true
    }
  },
  actions: {
    async fetchPersonalChatBinding() {
      const owner = this.draftOwnerUserId
      const { data } = await api.get('/assistant')
      if (this.draftOwnerUserId === owner && !this.personalChatSessionId) {
        this.personalChatSessionId = data.sessionId || null
      }
    },
    async ensurePersonalChat() {
      if (this.personalChatSessionId) return this.personalChatSessionId
      const owner = this.draftOwnerUserId
      const { data } = await api.post('/assistant/chat')
      if (this.draftOwnerUserId !== owner) return null
      this.personalChatSessionId = data.sessionId
      return data.sessionId
    },
    ensureSessionList(projectId = null) {
      const scope = sessionScopeKey(projectId)
      if (!this.sessionLists[scope]) this.sessionLists[scope] = emptySessionList()
      return this.sessionLists[scope]
    },
    findCachedSession(id) {
      return this.cachedSessions.find((session) => session.id === id) || null
    },
    updateCachedSession(id, update) {
      this.updateCachedSessions([{ id }], (item) => update(item))
    },
    updateCachedSessions(summaries, update = mergeSessionSummary) {
      const updates = new Map(summaries.map((summary) => [summary.id, summary]))
      const apply = (items) => {
        let matched = false
        let changed = false
        let reorder = false
        for (let index = 0; index < items.length; index += 1) {
          const previous = items[index]
          const summary = updates.get(previous.id)
          if (!summary) continue
          matched = true
          const next = update(previous, summary)
          if (Object.keys(next).every((key) => next[key] === previous[key])) continue
          reorder ||= compareSessions(previous, next) !== 0
          items[index] = next
          changed = true
        }
        return { matched, changed, items: reorder ? sortSessions(items) : items }
      }
      for (const list of Object.values(this.sessionLists)) {
        const result = apply(list.items)
        if (result.changed) {
          list.version += 1
          list.items = result.items
        }
      }
      const result = apply(this.sessionCatalog)
      // Server summaries also arrive directly from a tab refresh.
      if (result.changed || (result.matched && update === mergeSessionSummary)) this.sessionCatalogVersion += 1
      if (result.changed) this.sessionCatalog = result.items
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
      for (const [sessionId, selectedId] of Object.entries(this.drafts.sessionAgentProfiles)) {
        if (selectedId === profileId) this.drafts.sessionAgentProfiles[sessionId] = replacementId
      }
      this.persistDrafts()
    },
    removeCachedSession(id, { preserveTab = false } = {}) {
      if (!preserveTab) useSessionTabsStore().close(id)
      if (this.personalChatSessionId === id) this.personalChatSessionId = null
      this.sessionDetails = this.sessionDetails.filter((session) => session.id !== id)
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
          await saveChatOutboxEntries(snapshot.entries.filter((entry) => !entry.serverQueued).map((entry) => toRaw(entry)))
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
      useSessionTabsStore().removeProject(projectId, [...sessionIds])
      for (const id of sessionIds) {
        this.removeCachedSession(id, { preserveTab: true })
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
