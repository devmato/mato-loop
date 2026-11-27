import { markRaw, toRaw } from 'vue'
import { api } from 'src/boot/axios'
import { useApprovalStore } from 'src/stores/approvals'
import { controlCenterRequestState, useControlCenterStore } from 'src/stores/control-center'
import { useQuestionStore } from 'src/stores/questions'
import { downloadMessageAttachmentFiles, releaseAttachmentDrafts } from 'src/features/chat/attachments'
import { compareSessions, defaultSessionOptions, lastAssistantMessage, mergeSessionSummary, sanitizedSessionOptions, sessionBeforeUserMessage, sessionCreatePayload, sessionScopeKey, sessionScopeParams, sortSessions } from 'src/features/chat/state'
import { deleteChatOutboxEntries, saveChatOutboxEntries } from 'src/features/chat/outbox-storage'
import { notifyError } from 'src/utils/notifications'
import { errorDescriptor } from 'src/utils/api-errors'
import { SESSION_PAGE_SIZE, ACTIVE_TITLE_GENERATION_STATUSES, compareMessages, mergeMessages, sessionStateAfterMutation } from 'src/features/chat/model'
import { useSessionTabsStore } from './session-tabs-store'
import { useUserPreferencesStore } from 'src/features/settings/user-preferences-store'

const summarySyncs = new WeakMap()
const SESSION_CACHE_LIMIT = 180
const REOPEN_HISTORY_PAGE_LIMIT = 2

const sessionAccessFailed = error => [401, 403, 404, 410].includes(error?.response?.status)

function reconcileSummaryRows(rows, requested, replacements, insertMissing = false) {
  let next = null
  let reorder = false
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index]
    let updated = row
    if (requested.has(row.id)) {
      const summary = replacements.get(row.id)
      if (insertMissing) replacements.delete(row.id)
      updated = summary ? mergeSessionSummary(row, summary) : null
      if (updated !== row) {
        next ||= rows.slice(0, index)
        reorder ||= updated !== null && compareSessions(row, updated) !== 0
      }
    }
    if (next && updated) next.push(updated)
  }
  if (insertMissing && replacements.size) {
    next ||= rows.slice()
    next.push(...replacements.values())
    reorder = true
  }
  return reorder ? sortSessions(next) : next || rows
}

export const sessionsActions = {
    syncSessionSummaries(ids, config = {}) {
      let sync = summarySyncs.get(this)
      if (sync && sync.owner === this.draftOwnerUserId) {
        ids.forEach((id) => sync.pending.add(id))
        return sync.promise
      }
      sync = { pending: new Set(ids), promise: null, owner: this.draftOwnerUserId }
      const owner = this.draftOwnerUserId
      sync.promise = Promise.resolve().then(async () => {
        while (sync.pending.size && owner === this.draftOwnerUserId) {
          await Promise.all(Object.values(this.sessionLists).flatMap((list) => [list.loadRequest, list.pageRequest, list.refreshRequest]).filter(Boolean).map((request) => request.catch(() => {})))
          if (owner !== this.draftOwnerUserId) return
          // Include hints received while waiting for the current pages, before starting the GET.
          const requested = [...sync.pending]
          sync.pending.clear()
          const summaries = []
          for (let offset = 0; offset < requested.length; offset += 100) {
            const batch = requested.slice(offset, offset + 100)
            const params = new URLSearchParams({ pageSize: '100', includeArchived: 'true', view: 'status' })
            batch.forEach((id) => params.append('sessionIds', id))
            let data
            try {
              ({ data } = await api.get('/sessions', { ...config, params }))
            } catch (error) {
              if (owner === this.draftOwnerUserId && sessionAccessFailed(error)) this.evictSessionTranscripts(batch, error)
              throw error
            }
            if (owner !== this.draftOwnerUserId) return
            const available = new Set(data.items.map(item => item.id))
            this.evictSessionTranscripts(batch.filter(id => !available.has(id)))
            summaries.push(...data.items)
          }
          if (owner !== this.draftOwnerUserId) return
          // Pages may have started after the summary GET; apply only after they settle.
          await Promise.all(Object.values(this.sessionLists).flatMap((list) => [list.loadRequest, list.pageRequest, list.refreshRequest]).filter(Boolean).map((request) => request.catch(() => {})))
          if (owner !== this.draftOwnerUserId) return
          const updates = new Map(summaries.map((summary) => [summary.id, summary]))
          const changedIds = new Set(requested)
          for (const [scope, list] of Object.entries(this.sessionLists)) {
            const incoming = summaries.filter((item) => sessionScopeKey(item.projectId) === scope && !item.archived)
            if (!this.visibleSessionScopes.includes(scope)) {
              if (incoming.length || list.items.some(item => changedIds.has(item.id))) list.stale = true
              continue
            }
            if (!list.loaded) continue
            // Preserve the original keyset boundary when its row moves or disappears.
            const boundary = list.boundary || list.items.at(-1)
            const withinPage = (item) => !list.nextCursor || !boundary || compareSessions(item, boundary) <= 0
            const additions = incoming.filter((item) => withinPage(item) && item.id !== this.personalChatSessionId)
            const replacements = new Map(additions.map((item) => [item.id, item]))
            const items = reconcileSummaryRows(list.items, changedIds, replacements, true)
            if (items === list.items) continue
            list.version += 1
            list.boundary ||= boundary ? { ...boundary } : null
            list.items = items
          }
          const catalog = reconcileSummaryRows(this.sessionCatalog, changedIds, updates)
          // An equal response for a cached row must still invalidate an older catalog fetch.
          if (catalog !== this.sessionCatalog || catalog.some(item => changedIds.has(item.id))) this.sessionCatalogVersion += 1
          if (catalog !== this.sessionCatalog) this.sessionCatalog = catalog
          const tabs = useSessionTabsStore()
          const tabIds = tabs.items.map(tab => tab.sessionId).filter(id => changedIds.has(id))
          if (tabIds.length) {
            tabs.applySummaryUpdates(tabIds, Object.fromEntries(tabIds.map(id => [id, updates.get(id)])))
          }
        }
      }).finally(() => { if (summarySyncs.get(this) === sync) summarySyncs.delete(this) })
      summarySyncs.set(this, sync)
      return sync.promise
    },
    refreshTabSummaries() {
      const owner = this.draftOwnerUserId
      return useSessionTabsStore().refreshSummaries((ids, error) => {
        if (owner === this.draftOwnerUserId && (!error || sessionAccessFailed(error))) this.evictSessionTranscripts(ids, error)
      })
    },
    evictSessionTranscripts(ids, error = { response: { status: 404, data: { error: { code: 'session.notFound' } } } }) {
      if (!ids.length) return
      const unavailable = new Set(ids)
      if (this.sessionDetails.some(session => unavailable.has(session.id))) {
        this.sessionDetails = this.sessionDetails.filter(session => !unavailable.has(session.id))
      }
      if (this.current && unavailable.has(this.current.id)) this.clearFailedSessionLoad(this.current.id, error)
    },
    async invalidateSessionLists(config = {}) {
      const requests = []
      for (const [scope, list] of Object.entries(this.sessionLists)) {
        list.stale = true
        if ((list.loaded || list.loadRequest) && this.visibleSessionScopes.includes(scope)) {
          requests.push((async () => {
            if (list.loadRequest) await list.loadRequest.catch(() => {})
            list.stale = true
            await this.reconcileSessionList(scope === 'standalone' ? null : scope, config)
          })())
        }
      }
      await Promise.all(requests)
    },
    async reconcileSessionList(projectId = null, config = {}) {
      const list = this.ensureSessionList(projectId)
      if (list.loadRequest) await list.loadRequest.catch(() => {})
      // Full recovery without concrete IDs closes unknown gaps across loaded pages.
      if (list.stale) await this.refreshSessions(config, projectId)
    },
    async forkSession() {
      const source = this.current
      if (!source || source.readOnly || source.archivedAt) {
        notifyError({}, 'Öffne eine aktive Hauptsession, um sie zu kopieren.')
        return null
      }
      if (this.loading || this.historyActionPending) return null
      const owner = this.draftOwnerUserId
      this.historyActionPending = true
      try {
        const { data } = await api.post(`/sessions/${source.id}/fork`)
        if (this.draftOwnerUserId !== owner) return null
        if (this.current?.id === source.id && this.selectedSessionId === source.id) {
          if (this.drafts.sessionMessages[source.id]?.trim().toLowerCase() === '/fork') {
            delete this.drafts.sessionMessages[source.id]
          }
          this.persistDrafts()
          this.createdSessionResult = { sessionId: data.id, created: true }
        }
        return { sessionId: data.id, created: true }
      } finally {
        this.historyActionPending = false
      }
    },
    async createEmptySession(controlCenterState) {
      if (this.current || this.selectedSessionId) return this.current
      if (this.emptySessionPromise) return this.emptySessionPromise
      if (this.activeQueuedMessages.length || this.outboxSaving || this.queueDrainPromise) return null
      const version = this.sessionLoadVersion
      const projectId = this.sessionsProjectId
      const scope = sessionScopeKey(projectId)
      const draft = this.drafts.newSession
      const request = (async () => {
        if (this.draftPreviewRequests[scope]) {
          await this.waitForDraftDefaults(projectId)
          if (this.sessionLoadVersion !== version || this.sessionsProjectId !== projectId || this.drafts.newSession !== draft) return null
        }
        const { data } = await api.post('/sessions', {
          content: null,
          ...sessionCreatePayload(
            sanitizedSessionOptions(draft.options), projectId,
            controlCenterRequestState(controlCenterState)
          )
        })
        const session = { ...data.session, messages: [], contextSummaries: [] }
        const list = this.ensureSessionList(projectId)
        list.version += 1
        list.items = sortSessions([session, ...list.items.filter((item) => item.id !== session.id)])
        this.drafts.sessionMessages[session.id] = draft.message
        this.attachmentDrafts.sessionMessages[session.id] = this.attachmentDrafts.newSessions[scope] || []
        const dictation = this.getAudioDictation(null, projectId)
        if (dictation) this.setAudioDictation(dictation, session.id)
        this.attachmentDrafts.newSessions[scope] = []
        this.resetNewDraft(projectId)
        if (this.sessionLoadVersion === version && !this.current && !this.selectedSessionId) {
          this.current = session
          this.selectedSessionId = session.id
          useControlCenterStore().syncSession(session)
          this.createdSessionResult = {
            sessionId: session.id,
            appId: controlCenterState.activeAppId,
            created: true
          }
        }
        return session
      })()
      this.emptySessionPromise = markRaw(request)
      this.sessionSettingsPending += 1
      try {
        return await request
      } finally {
        if (this.emptySessionPromise === request) this.emptySessionPromise = null
        this.sessionSettingsPending = Math.max(0, this.sessionSettingsPending - 1)
      }
    },
    async setSessionScope(projectId = null) {
      const normalizedProjectId = projectId || null
      const targetList = this.ensureSessionList(normalizedProjectId)
      if (this.sessionsProjectId === normalizedProjectId && targetList.loaded && !targetList.stale) return
      if (
        this.sessionsProjectId !== normalizedProjectId
        && !this.current
        && this.selectedSessionId === null
      ) {
        this.sessionRequestController?.abort()
        this.sessionLoadVersion += 1
        if (this.running && !this.runId) {
          this.running = false
          this.stopping = false
          this.cancelRequested = false
          this.clearActivity()
        }
      }
      const previousScope = sessionScopeKey(this.sessionsProjectId)
      this.drafts.newSessions[previousScope] = this.drafts.newSession
      this.attachmentDrafts.newSessions[previousScope] = this.attachmentDrafts.newSession
      this.audioDictations.newSessions[previousScope] = this.audioDictations.newSession
      this.sessionsProjectId = normalizedProjectId
      const scope = sessionScopeKey(normalizedProjectId)
      if (!this.drafts.newSessions[scope]) {
        this.drafts.newSessions[scope] = {
          message: '',
          options: defaultSessionOptions()
        }
      }
      this.drafts.newSession = this.drafts.newSessions[scope]
      if (!this.attachmentDrafts.newSessions[scope]) this.attachmentDrafts.newSessions[scope] = []
      this.attachmentDrafts.newSession = this.attachmentDrafts.newSessions[scope]
      if (!Object.hasOwn(this.audioDictations.newSessions, scope)) {
        this.audioDictations.newSessions[scope] = null
      }
      this.audioDictations.newSession = this.audioDictations.newSessions[scope]
      this.persistDrafts()
      if (!targetList.loaded || targetList.stale) await this.fetchSessions(null, normalizedProjectId)
    },
    async fetchSessions(config = null, projectId = this.sessionsProjectId) {
      const list = this.ensureSessionList(projectId)
      if (list.loadRequest) return list.loadRequest
      const version = ++list.version
      list.loading = true
      list.loadFailed = false
      const request = (async () => {
        const preferences = useUserPreferencesStore()
        if (!preferences.loaded && !preferences.loadFailed) await preferences.fetchSettings().catch(() => {})
        if (version !== list.version) return
        const response = await api.get('/sessions', {
          ...(config || {}),
          params: {
            ...(config?.params || {}),
            ...sessionScopeParams(projectId),
            pageSize: preferences.sessionsPageSize
          }
        })
        if (version !== list.version) return
        list.items = sortSessions(response.data.items)
        list.nextCursor = response.data.nextCursor
        list.boundary = list.items.at(-1) ? { ...list.items.at(-1) } : null
        list.stale = false
        list.loaded = true
        list.loadMoreFailed = false
      })()
      list.loadRequest = markRaw(request)
      try {
        await request
      } catch (error) {
        if (version === list.version) list.loadFailed = true
        throw error
      } finally {
        if (list.loadRequest === request) {
          list.loadRequest = null
          list.loading = false
        }
      }
    },
    async refreshSessions(config = null, projectId = this.sessionsProjectId) {
      const list = this.ensureSessionList(projectId)
      if (list.loadRequest) await list.loadRequest
      if (this.sessionLists[sessionScopeKey(projectId)] !== list) return
      if (!list.loaded) return this.fetchSessions(config, projectId)
      if (list.refreshQueued) return list.refreshQueued.promise
      const queued = { promise: null }
      const version = ++list.version
      const request = (list.refreshRequest || Promise.resolve()).catch(() => {}).then(async () => {
        if (list.refreshQueued === queued) list.refreshQueued = null
        if (version !== list.version) return
        const targetCount = Math.max(useUserPreferencesStore().sessionsPageSize, list.items.length)
        const sessions = []
        let cursor = null
        do {
          const pageSize = Math.min(100, targetCount - sessions.length)
          const response = await api.get('/sessions', {
            ...(config || {}),
            params: {
              ...(config?.params || {}),
              ...sessionScopeParams(projectId),
              pageSize,
              cursor: cursor || undefined
            }
          })
          if (version !== list.version) return
          sessions.push(...response.data.items)
          cursor = response.data.nextCursor
        } while (cursor && sessions.length < targetCount)
        list.items = sortSessions([...new Map(sessions.map((session) => [session.id, session])).values()])
        list.nextCursor = cursor
        list.boundary = list.items.at(-1) ? { ...list.items.at(-1) } : null
        list.stale = false
        list.loadMoreFailed = false
      })
      queued.promise = markRaw(request)
      list.refreshQueued = markRaw(queued)
      list.refreshRequest = markRaw(request)
      try {
        await request
        if (version !== list.version && list.refreshRequest === request
          && this.sessionLists[sessionScopeKey(projectId)] === list) {
          list.refreshRequest = null
          return this.refreshSessions(config, projectId)
        }
      } finally {
        if (list.refreshRequest === request) list.refreshRequest = null
      }
    },
    async fetchMoreSessions(config = null, projectId = this.sessionsProjectId) {
      const list = this.ensureSessionList(projectId)
      const cursor = list.nextCursor
      if (!cursor) return
      if (list.pageRequest) return list.pageRequest
      const version = ++list.version
      list.loadingMore = true
      list.loadMoreFailed = false
      const request = (async () => {
        const response = await api.get('/sessions', {
          ...(config || {}),
          params: {
            ...(config?.params || {}),
            ...sessionScopeParams(projectId),
            pageSize: useUserPreferencesStore().sessionsPageSize,
            cursor
          }
        })
        if (version !== list.version || cursor !== list.nextCursor) return
        const sessions = new Map(list.items.map((session) => [session.id, session]))
        for (const session of response.data.items) sessions.set(session.id, session)
        list.items = sortSessions([...sessions.values()])
        list.nextCursor = response.data.nextCursor
        list.boundary = list.items.at(-1) ? { ...list.items.at(-1) } : null
      })()
      list.pageRequest = markRaw(request)
      try {
        await request
      } catch (error) {
        if (version === list.version) list.loadMoreFailed = true
        throw error
      } finally {
        if (list.pageRequest === request) list.pageRequest = null
        list.loadingMore = false
      }
    },
    async fetchSessionCatalog(config = null) {
      const version = ++this.sessionCatalogVersion
      const sessions = []
      let cursor = null
      do {
        const response = await api.get('/sessions', {
          ...(config || {}),
          params: { ...(config?.params || {}), includeArchived: true, pageSize: 100, cursor: cursor || undefined }
        })
        sessions.push(...response.data.items)
        cursor = response.data.nextCursor
      } while (cursor)
      if (version !== this.sessionCatalogVersion) return this.sessionCatalog
      this.sessionCatalog = sortSessions(sessions)
      this.pruneOrphanedSessionState()
      return this.sessionCatalog
    },
    retrySessionPagination(projectId = this.sessionsProjectId) {
      this.ensureSessionList(projectId).loadMoreFailed = false
    },
    pruneOrphanedSessionState() {
      const sessionIds = new Set(this.sessionCatalog.map((session) => session.id))
      for (const id of Object.keys(this.drafts.sessionMessages)) {
        if (!sessionIds.has(id)) delete this.drafts.sessionMessages[id]
      }
      for (const id of Object.keys(this.drafts.sessionAgentProfiles)) {
        if (!sessionIds.has(id)) delete this.drafts.sessionAgentProfiles[id]
      }
      for (const id of Object.keys(this.attachmentDrafts.sessionMessages)) {
        if (!sessionIds.has(id)) this.clearAttachmentDraft(id)
      }
      for (const id of Object.keys(this.audioDictations.sessionMessages)) {
        if (!sessionIds.has(id)) delete this.audioDictations.sessionMessages[id]
      }
      for (const id of Object.keys(this.queuedMessages)) {
        if (!id.startsWith('new:') && id !== this.current?.id && !sessionIds.has(id)) {
          void this.clearQueuedMessages(id)
        }
      }
      this.persistDrafts()
    },
    async refreshGeneratedTitle(sessionId) {
      for (const delay of [1000, 2000, 4000, 8000, 15000, 30000]) {
        await new Promise((resolve) => setTimeout(resolve, delay))
        const localSession = this.current?.id === sessionId
          ? this.current
          : this.findCachedSession(sessionId)
        if (!ACTIVE_TITLE_GENERATION_STATUSES.has(localSession?.titleGenerationStatus)) return
        try {
          await this.refreshSessions(null, localSession?.projectId || null)
        } catch {
          continue
        }
        const session = this.findCachedSession(sessionId)
        if (!session) continue
        if (this.current?.id === sessionId) {
          this.current.title = session.title
          this.current.titleGenerationStatus = session.titleGenerationStatus
        }
        if (!ACTIVE_TITLE_GENERATION_STATUSES.has(session.titleGenerationStatus)) return
      }
    },
    async loadSession(id) {
      if (id && this.selectedSessionId === id && this.loading && this.sessionLoadPromise) {
        return this.sessionLoadPromise
      }
      this.sessionRequestController?.abort()
      this.selectedSessionId = id
      this.sessionLoadPromise = null
      this.sessionLoadVersion += 1
      this.scheduleQueueDrain(0)
      const loadVersion = this.sessionLoadVersion
      const controller = id ? markRaw(new AbortController()) : null
      this.sessionRequestController = controller
      this.loading = Boolean(id)
      const tabs = useSessionTabsStore()
      const known = [this.findCachedSession(id), tabs.summaries[id]]
      const cached = id && this.draftOwnerUserId && !known.some(session => session?.archived || session?.archivedAt || session?.readOnly)
        ? this.sessionDetails.find(session => session.id === id && Array.isArray(session.messages)
          && !session.archived && !session.readOnly)
        : null
      this.current = cached ? { ...cached, messages: [...cached.messages], contextSummaries: [...(cached.contextSummaries || [])] } : null
      this.olderMessagesLoading = false
      this.olderMessagesLoadFailed = false
      this.olderMessagesPromise = null
      this.loadError = null
      const controlCenter = useControlCenterStore()
      if (id) {
        controlCenter.bindSession(this.current || this.findCachedSession(id) || { id })
      } else {
        controlCenter.resetDraft()
      }
      if (this.runSessionId !== id || (!id && this.running && !this.runId)) {
        clearTimeout(this.reconnectTimer)
        this.reconnectTimer = null
        this.reconnectAttempts = 0
        this.eventSource?.close()
        this.eventSource = null
        this.runSessionId = null
        this.runId = null
        this.runAgentProfileId = null
        this.running = false
        this.stopping = false
        this.cancelRequested = false
        this.streamContent = ''
        this.streamResearchSources = []
        this.streamReasoning = null
        this.streamContextSummary = null
        this.streamModelName = null
        this.processOutputs = {}
        this.completedToolCallId = null
        this.clearActivity()
      }
      if (!id) {
        return
      }
      const stillSelected = () => !controller.signal.aborted && this.selectedSessionId === id
        && this.sessionLoadVersion === loadVersion
      const request = (async () => {
        try {
          const { data: session } = await api.get(`/sessions/${id}`, {
            signal: controller.signal, params: { historyPreview: true }
          })
          if (!stillSelected()) return
          if (session.readOnly && session.parentSessionId) {
            this.current = null
            this.sessionDetails = this.sessionDetails.filter(item => item.id !== id)
            return session
          }
          // Bridge nearby cached rows with contiguous authoritative pages. A
          // distant reading position is restored separately via a history window.
          const boundary = !session.archived && !session.readOnly && cached?.messages?.find(message => message.role !== 'tool')
          const seenCursors = new Set()
          let historyFailed = false
          while (boundary?.createdAt && session.nextMessageCursor && seenCursors.size < REOPEN_HISTORY_PAGE_LIMIT) {
            const cursor = session.nextMessageCursor
            if (seenCursors.has(cursor)) break
            seenCursors.add(cursor)
            const oldest = session.messages.find(message => message.id === cursor)
            if (!oldest || compareMessages(oldest, boundary) <= 0) break
            let data
            try {
              ({ data } = await api.get(`/sessions/${id}/messages`, {
                signal: controller.signal, params: { cursor, pageSize: SESSION_PAGE_SIZE }, suppressErrorToast: true
              }))
            } catch (error) {
              if (sessionAccessFailed(error)) throw error
              historyFailed = true
              break
            }
            if (!stillSelected()) return
            session.messages = mergeMessages(data.items, session.messages)
            session.contextSummaries = mergeMessages(data.contextSummaries, session.contextSummaries)
            session.nextMessageCursor = data.nextCursor || null
          }
          if (!stillSelected()) return
          if (cached) {
            const keys = new Map(cached.messages?.filter(message => message.renderId).map(message => [message.id, message.renderId]))
            session.messages = session.messages?.map(message => keys.has(message.id) ? { ...message, renderId: keys.get(message.id) } : message)
          }
          controlCenter.syncSession(session)
          this.applySessionResult(session)
          this.olderMessagesLoadFailed = historyFailed
          if (session.activeRun && this.runId !== session.activeRun.id) {
            this.watchRun(id, session.activeRun.id, session.activeRun)
          } else if (!session.activeRun && !this.running) {
            this.clearActivity()
          }
          return session
        } catch (error) {
          if (!stillSelected()) return
          this.clearFailedSessionLoad(id, error)
          throw error
        } finally {
          if (this.sessionRequestController === controller) this.sessionRequestController = null
          if (this.sessionLoadVersion === loadVersion) {
            this.loading = false
            this.sessionLoadPromise = null
          }
        }
      })()
      this.sessionLoadPromise = markRaw(request)
      return request
    },
    clearFailedSessionLoad(id, error) {
      this.sessionRequestController?.abort()
      if (this.runSessionId === id) this.clearRunTracking()
      this.current = null
      this.sessionDetails = this.sessionDetails.filter(session => session.id !== id)
      this.loadError = error
      this.loading = false
      this.sessionLoadPromise = null
    },
    async refreshMessage(sessionId, messageId) {
      if (!messageId) return this.refreshSession(sessionId)
      const version = this.sessionLoadVersion
      const key = `${sessionId}:${messageId}`
      const token = Symbol()
      this.messageRefreshRequests[key] = token
      try {
        const { data } = await api.get(`/sessions/${sessionId}/messages/${messageId}`, {
          params: { compact: true }
        })
        if (this.current?.id !== sessionId || this.sessionLoadVersion !== version) return
        if (this.messageRefreshRequests[key] === token) {
          this.current.messages = mergeMessages(this.current.messages, [data])
        }
        return this.current
      } finally {
        if (this.messageRefreshRequests[key] === token) delete this.messageRefreshRequests[key]
      }
    },
    async refreshSession(id, { suppressErrorToast = false, recoveryAnchor } = {}) {
      if (this.historyActionPending) return
      if (this.selectedSessionId === id && this.sessionLoadPromise) await this.sessionLoadPromise.catch(() => {})
      if (this.selectedSessionId !== id || this.loading) return
      if (this.sessionRefreshQueued?.id === id) {
        if (recoveryAnchor) this.sessionRefreshQueued.recoveryAnchor = recoveryAnchor
        return this.sessionRefreshQueued.promise
      }
      const queued = { id, promise: null, recoveryAnchor }
      const refresh = (this.sessionRefreshPromise || Promise.resolve()).catch(() => {}).then(async () => {
        if (this.sessionRefreshQueued === queued) this.sessionRefreshQueued = null
        if (this.selectedSessionId !== id || this.loading || this.historyActionPending) return
        this.sessionRequestController?.abort()
        this.sessionLoadVersion += 1
        const loadVersion = this.sessionLoadVersion
        const controller = markRaw(new AbortController())
        this.sessionRequestController = controller
        try {
          const session = (await api.get(`/sessions/${id}`, {
            signal: controller.signal,
            ...(suppressErrorToast ? { suppressErrorToast: true } : {})
          })).data
          if (this.historyActionPending || controller.signal.aborted) return
          if (this.selectedSessionId === id && this.sessionLoadVersion === loadVersion) {
            if (this.current?.id === id) {
              // After a disconnect, bridge a gap larger than the newest page.
              // Normal live updates fetch single messages and never enter this loop.
              const latest = queued.recoveryAnchor || [...(this.current.messages || [])].reverse().find((message) => message.role !== 'tool')
              let cursor = session.nextMessageCursor
              while (latest?.createdAt && cursor && !session.messages.some((message) => message.id === latest.id)
                && Date.parse(session.messages[0]?.createdAt) >= Date.parse(latest.createdAt)) {
                const { data } = await api.get(`/sessions/${id}/messages`, {
                  signal: controller.signal, params: { cursor, pageSize: SESSION_PAGE_SIZE }
                })
                if (controller.signal.aborted || this.selectedSessionId !== id || this.sessionLoadVersion !== loadVersion) return
                session.messages = mergeMessages(data.items, session.messages)
                session.contextSummaries = mergeMessages(data.contextSummaries, session.contextSummaries)
                if (cursor === data.nextCursor) break
                cursor = data.nextCursor
              }
              // Only explicit pagination moves the displayed lower boundary.
              const boundary = this.current.messages?.find(message => message.id === this.current.nextMessageCursor)
              if (boundary?.createdAt) {
                const included = message => compareMessages(message, boundary) >= 0
                const carriers = new Map()
                session.messages = session.messages.filter(message => {
                  if (message.role === 'tool') {
                    // A result can arrive after the boundary even when its carrier is older.
                    const carrier = carriers.get(message.toolCallId)?.findLast(candidate => (
                      !candidate.runId || !message.runId || candidate.runId === message.runId
                    ))
                    return carrier && included(carrier)
                  }
                  for (const { id } of message.toolCalls || []) {
                    if (!carriers.has(id)) carriers.set(id, [])
                    carriers.get(id).push(message)
                  }
                  return included(message)
                })
                session.contextSummaries = (session.contextSummaries || []).filter(included)
              }
              session.messages = mergeMessages(this.current.messages, session.messages)
              session.contextSummaries = mergeMessages(
                this.current.contextSummaries,
                session.contextSummaries
              )
              session.nextMessageCursor = this.current.nextMessageCursor
            }
            this.applySessionResult(session)
            useControlCenterStore().syncBrowserTab(session)
            this.loadError = null
            if (session.activeRun && this.runId !== session.activeRun.id) {
              this.watchRun(id, session.activeRun.id, session.activeRun)
            } else if (!session.activeRun && !this.running) {
              this.clearActivity()
            }
            return session
          }
        } catch (error) {
          if (controller.signal.aborted) return
          if (sessionAccessFailed(error) && this.selectedSessionId === id && this.sessionLoadVersion === loadVersion) {
            this.clearFailedSessionLoad(id, error)
          }
          throw error
        } finally {
          if (this.sessionRequestController === controller) this.sessionRequestController = null
        }
      })
      queued.promise = markRaw(refresh)
      this.sessionRefreshQueued = markRaw(queued)
      this.sessionRefreshPromise = markRaw(refresh)
      try {
        return await refresh
      } finally {
        if (this.sessionRefreshPromise === refresh) this.sessionRefreshPromise = null
      }
    },
    async fetchSession(id) {
      const { data } = await api.get(`/sessions/${id}`)
      return data
    },
    async fetchMessageWindow(sessionId, messageId, signal) {
      try {
        const { data } = await api.get(`/sessions/${sessionId}/messages/${messageId}/window`, {
          signal, suppressErrorToast: true
        })
        return data
      } catch (error) {
        if (errorDescriptor(error)?.code === 'session.messageNotFound') return null
        if (!signal?.aborted && this.current?.id === sessionId && sessionAccessFailed(error)) {
          this.clearFailedSessionLoad(sessionId, error)
        }
        throw error
      }
    },
    async fetchOlderMessages(id, cursor, pageSize, signal) {
      const version = this.sessionLoadVersion
      try {
        const { data } = await api.get(`/sessions/${id}/messages`, {
          params: { cursor, pageSize }, ...(signal ? { signal } : {}), suppressErrorToast: true
        })
        return data
      } catch (error) {
        if (!signal?.aborted && this.current?.id === id && this.sessionLoadVersion === version && sessionAccessFailed(error)) {
          this.clearFailedSessionLoad(id, error)
        }
        throw error
      }
    },
    async loadOlderMessages(id = this.current?.id, pageSize = SESSION_PAGE_SIZE) {
      if (this.loading) return []
      const session = this.current
      const cursor = session?.id === id ? session.nextMessageCursor : null
      if (this.olderMessagesPromise) return await this.olderMessagesPromise
      if (!id || !cursor) return []
      this.olderMessagesLoading = true
      this.olderMessagesLoadFailed = false
      const request = (async () => {
        const data = await this.fetchOlderMessages(id, cursor, pageSize)
        if (this.olderMessagesPromise !== request || this.current?.id !== id || this.current.nextMessageCursor !== cursor) return []
        const existingIds = new Set((this.current.messages || []).map((message) => message.id))
        const added = (data.items || []).filter((message) => !existingIds.has(message.id))
        const existingSummaryIds = new Set(
          (this.current.contextSummaries || []).map((summary) => summary.id)
        )
        const addedSummaries = (data.contextSummaries || [])
          .filter((summary) => !existingSummaryIds.has(summary.id))
        this.current.messages = mergeMessages(added, this.current.messages)
        this.current.contextSummaries = [
          ...addedSummaries,
          ...(this.current.contextSummaries || [])
        ]
        this.current.nextMessageCursor = data.nextCursor || null
        if (!this.sessionRefreshPromise && !this.current.activeRun && !this.running) this.cacheSessionWindow(this.current)
        return added
      })()
      this.olderMessagesPromise = markRaw(request)
      try {
        return await request
      } catch (error) {
        if (this.olderMessagesPromise === request) this.olderMessagesLoadFailed = true
        throw error
      } finally {
        if (this.olderMessagesPromise === request) {
          this.olderMessagesLoading = false
          this.olderMessagesPromise = null
        }
      }
    },
    async updateActive(patch, { draftOnly = false } = {}) {
      if (this.loading) return
      if (this.current) {
        const sessionId = this.current.id
        const profileSelection = Object.hasOwn(patch, 'agent_profile_id') || Object.hasOwn(patch, 'session_template_id')
        const selectionVersion = profileSelection
          ? (this.profileSelectionVersions[sessionId] || 0) + 1 : null
        if (profileSelection) this.profileSelectionVersions[sessionId] = selectionVersion
        if (
          Object.hasOwn(patch, 'agent_profile_id')
          && (draftOnly || this.running || this.stopping || this.runId || this.activeQueuedMessages.length)
        ) {
          this.drafts.sessionAgentProfiles[sessionId] = patch.agent_profile_id
          this.persistDrafts()
          return
        }
        await this.updateCurrent(patch)
        if (profileSelection && this.profileSelectionVersions[sessionId] !== selectionVersion) return
        if (Object.hasOwn(patch, 'agent_profile_id')) {
          this.drafts.sessionAgentProfiles[sessionId] = patch.agent_profile_id
          this.persistDrafts()
        } else if (Object.hasOwn(patch, 'session_template_id')) {
          delete this.drafts.sessionAgentProfiles[sessionId]
          this.persistDrafts()
        }
        return
      }
      if (Object.hasOwn(patch, 'session_template_id')) {
        await this.fetchDefaults(this.sessionsProjectId, patch.session_template_id)
        return
      }
      const options = this.drafts.newSession.options
      const fields = {
        session_profile: 'sessionProfile',
        agent_profile_id: 'agentProfileId',
        model_id: 'modelId',
        reasoning_effort: 'reasoningEffort',
        memory_mode: 'memoryMode',
        trust_level: 'trustLevel',
        web_search_policy: 'webSearchPolicy',
        preferred_execution_agent_id: 'preferredExecutionAgentId',
        current_directory: 'currentDirectory',
        workspace_mode: 'workspaceMode',
        workspace_path: 'workspacePath',
        git_fetch_before_actions: 'gitFetchBeforeActions'
      }
      const normalizedPatch = { ...patch }
      if (Object.hasOwn(patch, 'preferred_execution_agent_id') || Object.hasOwn(patch, 'current_directory')) normalizedPatch.workspace_path = null
      if (typeof normalizedPatch.session_profile === 'function') {
        normalizedPatch.session_profile = normalizedPatch.session_profile(options.sessionProfile)
      }
      if (
        Object.hasOwn(patch, 'preferred_execution_agent_id')
        && patch.preferred_execution_agent_id !== options.preferredExecutionAgentId
      ) {
        if (!Object.hasOwn(patch, 'current_directory')) normalizedPatch.current_directory = null
      }
      for (const [field, value] of Object.entries(normalizedPatch)) {
        if (fields[field]) options[fields[field]] = value
      }
      Object.assign(options, sanitizedSessionOptions(options))
      if (['model_id', 'agent_profile_id', 'session_profile', 'memory_mode', 'reasoning_effort'].some((field) => Object.hasOwn(patch, field))) await this.fetchDefaults()
      this.persistDrafts()
    },
    async updateCurrent(patch) {
      const session = this.current
      if (!session || this.loading) return
      const normalizedPatch = { ...patch }
      if (
        Object.hasOwn(patch, 'preferred_execution_agent_id')
        && patch.preferred_execution_agent_id !== session.preferredExecutionAgentId
      ) {
        if (!Object.hasOwn(patch, 'current_directory')) normalizedPatch.current_directory = null
      }
      this.sessionSettingsPending += 1
      const request = (this.sessionSettingsPromise || Promise.resolve()).catch(() => {}).then((previous) => {
        const latest = previous?.data?.id === session.id ? previous.data : this.current?.id === session.id ? this.current : session
        const resolvedPatch = { ...normalizedPatch }
        if (typeof resolvedPatch.session_profile === 'function') {
          resolvedPatch.session_profile = resolvedPatch.session_profile(latest.sessionProfile)
        }
        return api.patch(`/sessions/${session.id}`, resolvedPatch)
      })
      this.sessionSettingsPromise = markRaw(request)
      try {
        const { data } = await request
        if (this.current?.id === session.id && this.selectedSessionId === session.id) {
          this.current = { ...this.current, ...data }
        }
        this.updateCachedSession(data.id, (item) => ({ ...item, ...data }))
        return data
      } finally {
        if (this.sessionSettingsPromise === request) this.sessionSettingsPromise = null
        this.sessionSettingsPending = Math.max(0, this.sessionSettingsPending - 1)
      }
    },
    async setTodosCollapsed(id, collapsed) {
      const { data } = await api.patch(`/sessions/${id}`, { todos_collapsed: collapsed })
      this.updateCachedSession(id, (session) => ({ ...session, todosCollapsed: data.todosCollapsed }))
      if (this.current?.id === id) this.current.todosCollapsed = data.todosCollapsed
    },
    async renameSession(id, title) {
      const { data } = await api.patch(`/sessions/${id}`, { title })
      this.updateCachedSession(id, (session) => ({ ...session, ...data }))
      if (this.current?.id === id) this.current = { ...this.current, ...data }
    },
    async moveSession(id, projectId, { newProjectName, applyProjectDefaults = false } = {}) {
      const catalogHadSession = this.sessionCatalog.some((session) => session.id === id)
      const { data } = await api.patch(`/sessions/${id}`, newProjectName
        ? { new_project_name: newProjectName }
        : { project_id: projectId || null, ...(applyProjectDefaults && projectId ? { apply_project_defaults: true } : {}) })
      const targetProjectId = data.projectId || null
      const summary = { ...this.findCachedSession(id), ...data }
      const queued = this.queuedMessages[id] || []
      for (const entry of queued) {
        entry.projectId = targetProjectId
      }
      if (queued.length) {
        try {
          await saveChatOutboxEntries(queued.filter((entry) => !entry.serverQueued).map((entry) => toRaw(entry)))
        } catch {
          for (const entry of queued) entry.paused = true
          this.queuedSendPausedSessions[id] = true
          notifyError({}, 'Die vorgemerkten Nachrichten konnten lokal nicht aktualisiert werden.')
        }
      }
      this.removeCachedSession(id, { preserveTab: true })
      const tab = useSessionTabsStore().items.find(tab => tab.sessionId === id)
      if (tab) Object.assign(tab, { projectId: targetProjectId, title: data.title || tab.title })
      const targetList = this.sessionLists[sessionScopeKey(targetProjectId)]
      if (targetList?.loadRequest) {
        try {
          await targetList.loadRequest
        } catch {
          // The destination remains retryable when its independent list request failed.
        }
      }
      if (targetList?.loaded) {
        targetList.version += 1
        targetList.items = sortSessions([summary, ...targetList.items])
      }
      if (catalogHadSession) {
        this.sessionCatalogVersion += 1
        this.sessionCatalog = sortSessions([summary, ...this.sessionCatalog])
      }
      if (this.current?.id === id) this.current = { ...this.current, ...data }
      return data
    },
    async setSessionPinned(id, pinned) {
      const { data } = await api.patch(`/sessions/${id}`, { pinned })
      this.updateCachedSession(id, (session) => ({ ...session, ...data }))
      if (this.current?.id === id) this.current = { ...this.current, ...data }
    },
    async setArchived(id, archived) {
      const { data } = await api.post(`/sessions/${id}/archive`, { archived })
      this.removeCachedSession(id)
      if (this.current?.id === id) this.current = { ...this.current, ...data }
      await this.refreshSessions(null, data.projectId || null)
      return data
    },
    async deleteSession(id, { permanent = false, automationMode = null, automationImpact = null, worktreeImpact = null, keepWorktree = false, suppressErrorToast = false } = {}) {
      const config = {
        ...(suppressErrorToast ? { suppressErrorToast: true } : {}),
        params: {
          permanent,
          ...(automationMode ? { automationMode, automationImpact } : {}),
          ...(worktreeImpact ? { worktreeImpact } : {}),
          ...(keepWorktree ? { keepWorktree } : {})
        }
      }
      const queued = [...(this.queuedMessages[id] || [])]
      const ownerUserId = this.draftOwnerUserId
      const loadVersion = this.outboxLoadVersion
      if (queued.length) {
        try {
          await deleteChatOutboxEntries(queued.map((entry) => entry.id))
        } catch (error) {
          notifyError({}, 'Vorgemerkte Nachrichten konnten nicht gelöscht werden.')
          throw error
        }
      }
      try {
        await api.delete(`/sessions/${id}`, config)
      } catch (error) {
        if (
          queued.length
          && ownerUserId === this.draftOwnerUserId
          && loadVersion === this.outboxLoadVersion
        ) {
          try {
            await saveChatOutboxEntries(queued.filter((entry) => !entry.serverQueued).map((entry) => toRaw(entry)))
          } catch {
            notifyError({}, 'Vorgemerkte Nachrichten konnten nicht wiederhergestellt werden.')
          }
        }
        throw error
      }
      releaseAttachmentDrafts(queued.flatMap((entry) => entry.attachments))
      delete this.queuedMessages[id]
      delete this.queuedSendPausedSessions[id]
      this.removeCachedSession(id)
      delete this.drafts.sessionMessages[id]
      delete this.drafts.sessionAgentProfiles[id]
      this.clearAttachmentDraft(id)
      this.setAudioDictation(null, id)
      this.persistDrafts()
      if (this.current?.id === id) this.current = null
    },
    async sessionDeleteImpact(id, { suppressErrorToast = false } = {}) {
      const { data } = await api.get(
        `/sessions/${id}/delete-impact`,
        suppressErrorToast ? { suppressErrorToast: true } : undefined
      )
      return data
    },
    clearRunTracking() {
      const sessionId = this.runSessionId
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
      this.reconnectAttempts = 0
      this.eventSource?.close()
      this.eventSource = null
      this.runSessionId = null
      this.runId = null
      this.runAgentProfileId = null
      this.running = false
      this.stopping = false
      this.cancelRequested = false
      this.streamContent = ''
      this.streamResearchSources = []
      this.streamReasoning = null
      this.streamContextSummary = null
      this.runWarnings = []
      this.streamModelName = null
      this.processOutputs = {}
      this.completedToolCallId = null
      this.clearActivity()
      if (sessionId) this.setSessionRunStatus(sessionId, null)
    },
    setSessionRunStatus(sessionId, status) {
      this.updateCachedSession(sessionId, (session) => ({ ...session, activeRunStatus: status }))
    },
    updateSessionSummary(session) {
      if (session.archived) { this.removeCachedSession(session.id); return }
      const summary = { ...session }
      const lastUserMessage = [...(session.messages || [])].reverse().find((message) => message.role === 'user')
      if (lastUserMessage && !Object.hasOwn(session, 'lastUserMessageAt')) summary.lastUserMessageAt = lastUserMessage.createdAt
      delete summary.messages
      delete summary.contextSummaries
      delete summary.activeRun
      this.updateCachedSession(session.id, (item) => ({ ...item, ...summary }))
      const list = this.sessionLists[sessionScopeKey(summary.projectId)]
      if (!list?.loaded || list.items.some((item) => item.id === session.id)) return
      list.version += 1
      list.items = sortSessions([...list.items, summary])
    },
    applySessionResult(session) {
      this.current = session
      this.updateSessionSummary(session)
      this.cacheSessionWindow(session)
    },
    cacheSessionWindow(session) {
      const others = this.sessionDetails.filter(item => item.id !== session.id)
      const { messages, contextSummaries, nextMessageCursor } = session
      // Never slice a cursor window: the cursor would skip dropped carriers, and
      // a slice could separate an assistant section from its tool results.
      this.sessionDetails = !session.archived && !session.readOnly && Array.isArray(messages)
        && messages.length + (contextSummaries?.length || 0) <= SESSION_CACHE_LIMIT
        && (!nextMessageCursor || messages.some(message => message.id === nextMessageCursor))
        ? [{ ...session, messages: [...messages], contextSummaries: [...(contextSummaries || [])] }, ...others].slice(0, 5)
        : others
    },
    async markSessionRead(session) {
      if (!session?.id) return
      const message = lastAssistantMessage(session.messages)
      await api.post(
        `/sessions/${session.id}/read`,
        message ? { throughMessageId: message.id } : {},
        { suppressErrorToast: true }
      )
      if (message) {
        if (this.current?.id === session.id && lastAssistantMessage(this.current.messages)?.id !== message.id) return
        if (this.current?.id === session.id) this.current.hasUnreadMessages = false
        this.updateCachedSession(session.id, (summary) => ({ ...summary, hasUnreadMessages: false }))
      }
    },
    async undoUserMessage(message, { session = this.current, files, outboxId } = {}) {
      if (!session || message?.role !== 'user' || this.historyActionPending) return null
      const owner = this.draftOwnerUserId
      this.historyActionPending = true
      try {
        try {
          files ??= await downloadMessageAttachmentFiles(message)
        } catch (error) {
          notifyError(error, 'Die Anhänge konnten nicht wiederhergestellt werden.')
          throw error
        }
        if (owner !== this.draftOwnerUserId) return null
        const replacementError = this.attachmentReplacementError(
          files,
          session.id,
          session.id,
          outboxId
        )
        if (replacementError) {
          notifyError({}, replacementError)
          throw new Error(replacementError)
        }
        const restoreDraft = (content) => {
          const saved = this.setDraft(content, { sessionId: session.id })
          this.replaceAttachmentFiles(files, session.id, message.attachments)
          if (!saved && content) {
            notifyError({}, 'Der wiederhergestellte Text konnte nicht lokal gespeichert werden. Kopiere ihn, bevor du die Seite schließt oder neu lädst.')
          }
        }
        let data
        let recoveredSession = null
        let mutationKnownUnchanged = false
        try {
          data = (await api.post(`/sessions/${session.id}/messages/${message.id}/undo`, null, {
            suppressErrorToast: true
          })).data
        } catch (error) {
          const ambiguous = !error?.response || error.response.status >= 500
          if (ambiguous) {
            try {
              const recovered = await sessionStateAfterMutation(session.id, message.id)
              if (recovered.messageRemoved && recovered.session) {
                recoveredSession = recovered.session
                data = { draft: { content: message.content } }
              } else {
                mutationKnownUnchanged = true
              }
            } catch {
              // Keep the downloaded files recoverable when the server state cannot be determined.
            }
          }
          if (owner !== this.draftOwnerUserId) return null
          if (!data && ambiguous && !mutationKnownUnchanged) {
            restoreDraft(message.content)
          }
          if (!data) {
            notifyError(error)
            throw error
          }
        }
        if (owner !== this.draftOwnerUserId) return null
        const stillSelected = this.selectedSessionId === session.id && this.current?.id === session.id
        const updatedSession = recoveredSession || sessionBeforeUserMessage(
          stillSelected && this.current.messages.some((item) => item.id === message.id) ? this.current : session,
          message.id
        )
        if (stillSelected) {
          this.sessionRequestController?.abort()
          this.sessionLoadVersion += 1
          this.clearRunTracking()
        }
        delete this.drafts.sessionMessages[session.id]
        this.clearAttachmentDraft(session.id)
        this.setAudioDictation(null, session.id)
        if (stillSelected) this.applySessionResult(updatedSession)
        else this.updateSessionSummary(updatedSession)
        restoreDraft(data.draft.content)
        if (!recoveredSession) {
          const loadVersion = this.sessionLoadVersion
          await this.fetchSession(session.id).then((canonical) => {
            if (this.selectedSessionId === session.id && this.current?.id === session.id
              && this.sessionLoadVersion === loadVersion) {
              this.applySessionResult({
                ...canonical,
                messages: mergeMessages(this.current.messages, canonical.messages),
                contextSummaries: mergeMessages(this.current.contextSummaries, canonical.contextSummaries),
                nextMessageCursor: this.current.nextMessageCursor
              })
            } else this.updateSessionSummary(canonical)
          }).catch(() => {})
        }
        void useApprovalStore().fetchPending()
        void useQuestionStore().fetchPending().catch(() => {})
        return data
      } finally {
        this.historyActionPending = false
      }
    },
    async retryFailedMessage(message) {
      const session = this.current
      if (!session || this.loading || message?.kind !== 'runtime_error' || this.historyActionPending) return null
      this.historyActionPending = true
      try {
        let data
        let recoveredSession = null
        try {
          data = (await api.post(`/sessions/${session.id}/messages/${message.id}/retry`, null, {
            suppressErrorToast: true
          })).data
        } catch (error) {
          const ambiguous = !error?.response || error.response.status >= 500
          if (ambiguous) {
            try {
              const recovered = await sessionStateAfterMutation(session.id, message.id)
              if (recovered.messageRemoved && recovered.session) {
                recoveredSession = recovered.session
                data = {
                  runId: recovered.session.activeRun?.id || null,
                  modelName: recovered.session.activeRun?.modelName || null,
                  createdAt: recovered.session.activeRun?.createdAt || null,
                  activeDurationMs: recovered.session.activeRun?.activeDurationMs ?? null,
                  activeDurationRunning: recovered.session.activeRun?.activeDurationRunning === true,
                  activeTimingRevision: recovered.session.activeRun?.activeTimingRevision ?? null
                }
              }
            } catch {
              // The original request error remains canonical when recovery is unavailable.
            }
          }
          if (!data) {
            notifyError(error)
            throw error
          }
        }
        const activeRun = data.runId ? {
          id: data.runId,
          status: 'queued',
          modelName: data.modelName,
          agentProfileId: data.agentProfileId,
          createdAt: data.createdAt,
          activeDurationMs: data.activeDurationMs,
          activeDurationRunning: data.activeDurationRunning,
          activeTimingRevision: data.activeTimingRevision
        } : null
        // Retry deletes a suffix which may include our page cursor and inputs
        // outside this page. Replace canonical history instead of merging it.
        const version = this.sessionLoadVersion
        let updatedSession = recoveredSession
        try {
          updatedSession ||= (await api.get(`/sessions/${session.id}`)).data
        } catch (error) {
          if (this.current?.id === session.id && this.sessionLoadVersion === version) {
            this.sessionRequestController?.abort()
            this.sessionLoadVersion += 1
            this.clearRunTracking()
            this.current = null
            this.sessionDetails = this.sessionDetails.filter((item) => item.id !== session.id)
            this.loadError = error
          }
          throw error
        }
        const stillSelected = this.selectedSessionId === session.id && this.current?.id === session.id
          && this.sessionLoadVersion === version
        if (stillSelected) {
          this.sessionRequestController?.abort()
          this.sessionLoadVersion += 1
          this.clearRunTracking()
          this.applySessionResult(updatedSession)
          if (activeRun) this.watchRun(session.id, data.runId, activeRun)
        } else {
          this.updateSessionSummary(updatedSession)
        }
        void useApprovalStore().fetchPending()
        void useQuestionStore().fetchPending().catch(() => {})
        return data
      } finally {
        this.historyActionPending = false
      }
    },
}
