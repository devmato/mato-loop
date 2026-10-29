import { markRaw, toRaw } from 'vue'
import { api } from 'src/boot/axios'
import { useApprovalStore } from 'src/stores/approvals'
import { controlCenterRequestState, useControlCenterStore } from 'src/stores/control-center'
import { useQuestionStore } from 'src/stores/questions'
import { useInAppNotificationStore } from 'src/stores/in-app-notifications'
import { useProjectStore } from 'src/stores/projects'
import { downloadMessageAttachmentFiles, releaseAttachmentDrafts } from 'src/features/chat/attachments'
import { defaultSessionOptions, lastAssistantMessage, sanitizedSessionOptions, sessionBeforeFailedResponse, sessionBeforeUserMessage, sessionCreatePayload, sessionScopeKey, sessionScopeParams, sortSessions } from 'src/features/chat/state'
import { deleteChatOutboxEntries, saveChatOutboxEntries } from 'src/utils/chat-outbox'
import { notifyError } from 'src/utils/notifications'
import { SESSION_PAGE_SIZE, ACTIVE_TITLE_GENERATION_STATUSES, mergeMessages, sessionStateAfterMutation } from 'src/features/chat/model'

export const sessionsActions = {
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
      if (this.sessionsProjectId === normalizedProjectId && targetList.loaded) return
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
      if (!targetList.loaded) await this.fetchSessions(null, normalizedProjectId)
    },
    async fetchSessions(config = null, projectId = this.sessionsProjectId) {
      const list = this.ensureSessionList(projectId)
      if (list.loadRequest) return list.loadRequest
      const version = ++list.version
      list.loading = true
      list.loadFailed = false
      const request = (async () => {
        const response = await api.get('/sessions', {
          ...(config || {}),
          params: {
            ...(config?.params || {}),
            ...sessionScopeParams(projectId),
            pageSize: SESSION_PAGE_SIZE
          }
        })
        if (version !== list.version) return
        list.items = sortSessions(response.data.items)
        list.nextCursor = response.data.nextCursor
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
        const targetCount = Math.max(SESSION_PAGE_SIZE, list.items.length)
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
            pageSize: SESSION_PAGE_SIZE,
            cursor
          }
        })
        if (version !== list.version || cursor !== list.nextCursor) return
        const sessions = new Map(list.items.map((session) => [session.id, session]))
        for (const session of response.data.items) sessions.set(session.id, session)
        list.items = sortSessions([...sessions.values()])
        list.nextCursor = response.data.nextCursor
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
      this.sessionRequestController?.abort()
      this.selectedSessionId = id
      this.sessionLoadPromise = null
      this.sessionLoadVersion += 1
      this.scheduleQueueDrain(0)
      const loadVersion = this.sessionLoadVersion
      const controller = id ? markRaw(new AbortController()) : null
      this.sessionRequestController = controller
      this.current = null
      this.olderMessagesLoading = false
      this.olderMessagesLoadFailed = false
      this.olderMessagesPromise = null
      this.loadError = null
      const controlCenter = useControlCenterStore()
      if (id) {
        controlCenter.bindSession(this.findCachedSession(id) || { id })
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
        this.loading = false
        return
      }
      this.loading = true
      try {
        this.sessionLoadPromise = markRaw(api.get(`/sessions/${id}`, { signal: controller.signal }))
        const session = (await this.sessionLoadPromise).data
        if (this.selectedSessionId !== id || this.sessionLoadVersion !== loadVersion) return
        if (session.readOnly && session.parentSessionId) return session
        controlCenter.syncSession(session)
        this.applySessionResult(session)
        if (session.activeRun && this.runId !== session.activeRun.id) {
          this.watchRun(id, session.activeRun.id, session.activeRun)
        } else if (!session.activeRun && !this.running) {
          this.clearActivity()
        }
      } catch (error) {
        if (controller.signal.aborted || this.sessionLoadVersion !== loadVersion) return
        this.loadError = error
        throw error
      } finally {
        if (this.sessionRequestController === controller) this.sessionRequestController = null
        if (this.sessionLoadVersion === loadVersion) {
          this.loading = false
          this.sessionLoadPromise = null
        }
      }
    },
    async refreshSession(id, { suppressErrorToast = false } = {}) {
      if (this.selectedSessionId === id && this.sessionLoadPromise) await this.sessionLoadPromise.catch(() => {})
      if (this.selectedSessionId !== id || this.loading) return
      if (this.sessionRefreshQueued?.id === id) return this.sessionRefreshQueued.promise
      const queued = { id, promise: null }
      const refresh = (this.sessionRefreshPromise || Promise.resolve()).catch(() => {}).then(async () => {
        if (this.sessionRefreshQueued === queued) this.sessionRefreshQueued = null
        if (this.selectedSessionId !== id || this.loading) return
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
          if (this.selectedSessionId === id && this.sessionLoadVersion === loadVersion) {
            if (this.current?.id === id) {
              session.messages = mergeMessages(this.current.messages, session.messages)
              session.contextSummaries = mergeMessages(
                this.current.contextSummaries,
                session.contextSummaries
              )
              session.nextMessageCursor = this.current.nextMessageCursor
            }
            this.applySessionResult(session)
            this.loadError = null
            if (session.activeRun && this.runId !== session.activeRun.id) {
              this.watchRun(id, session.activeRun.id, session.activeRun)
            } else if (!session.activeRun && !this.running) {
              this.clearActivity()
            }
            return session
          }
        } catch (error) {
          if (!controller.signal.aborted) throw error
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
    async loadOlderMessages(id = this.current?.id) {
      const session = this.current
      const cursor = session?.id === id ? session.nextMessageCursor : null
      if (this.olderMessagesPromise) return await this.olderMessagesPromise
      if (!id || !cursor) return []
      this.olderMessagesLoading = true
      this.olderMessagesLoadFailed = false
      const request = (async () => {
        const { data } = await api.get(`/sessions/${id}/messages`, {
          params: { cursor, pageSize: SESSION_PAGE_SIZE }
        })
        if (this.current?.id !== id || this.current.nextMessageCursor !== cursor) return []
        const existingIds = new Set((this.current.messages || []).map((message) => message.id))
        const added = (data.items || []).filter((message) => !existingIds.has(message.id))
        const existingSummaryIds = new Set(
          (this.current.contextSummaries || []).map((summary) => summary.id)
        )
        const addedSummaries = (data.contextSummaries || [])
          .filter((summary) => !existingSummaryIds.has(summary.id))
        this.current.messages = [...added, ...(this.current.messages || [])]
        this.current.contextSummaries = [
          ...addedSummaries,
          ...(this.current.contextSummaries || [])
        ]
        this.current.nextMessageCursor = data.nextCursor || null
        return added
      })()
      this.olderMessagesPromise = markRaw(request)
      try {
        return await request
      } catch (error) {
        if (this.current?.id === id) this.olderMessagesLoadFailed = true
        throw error
      } finally {
        if (this.current?.id === id) this.olderMessagesLoading = false
        if (this.olderMessagesPromise === request) this.olderMessagesPromise = null
      }
    },
    async loadThroughMessage(messageId) {
      while (
        this.current
        && !this.current.messages?.some((message) => message.id === messageId)
        && this.current.nextMessageCursor
      ) {
        await this.loadOlderMessages(this.current.id)
      }
      return Boolean(this.current?.messages?.some((message) => message.id === messageId))
    },
    async updateActive(patch, { draftOnly = false } = {}) {
      if (this.current) {
        const sessionId = this.current.id
        if (
          Object.hasOwn(patch, 'agent_profile_id')
          && (draftOnly || this.running || this.stopping || this.runId || this.activeQueuedMessages.length)
        ) {
          this.drafts.sessionAgentProfiles[sessionId] = patch.agent_profile_id
          this.persistDrafts()
          return
        }
        await this.updateCurrent(patch)
        if (Object.hasOwn(patch, 'agent_profile_id') || Object.hasOwn(patch, 'session_template_id')) {
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
        workspace_mode: 'workspaceMode'
      }
      const normalizedPatch = { ...patch }
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
      if (!session) return
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
    async moveSession(id, projectId) {
      const catalogHadSession = this.sessionCatalog.some((session) => session.id === id)
      const { data } = await api.patch(`/sessions/${id}`, { project_id: projectId || null })
      const summary = { ...this.findCachedSession(id), ...data }
      const queued = this.queuedMessages[id] || []
      for (const entry of queued) {
        entry.projectId = projectId || null
      }
      if (queued.length) {
        try {
          await saveChatOutboxEntries(queued.map((entry) => toRaw(entry)))
        } catch {
          for (const entry of queued) entry.paused = true
          this.queuedSendPausedSessions[id] = true
          notifyError({}, 'Die vorgemerkten Nachrichten konnten lokal nicht aktualisiert werden.')
        }
      }
      this.removeCachedSession(id)
      const targetList = this.sessionLists[sessionScopeKey(projectId)]
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
    async deleteSession(id, { automationMode = null, automationImpact = null, removeWorktree = false, suppressErrorToast = false } = {}) {
      const config = {
        ...(suppressErrorToast ? { suppressErrorToast: true } : {}),
        ...(automationMode || removeWorktree ? { params: {
          ...(automationMode ? { automationMode, automationImpact } : {}),
          ...(removeWorktree ? { removeWorktree: true } : {})
        } } : {})
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
            await saveChatOutboxEntries(queued.map((entry) => toRaw(entry)))
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
      const notifications = useInAppNotificationStore()
      const projects = useProjectStore()
      await Promise.allSettled([
        notifications.fetchLatest({ suppressErrorToast: true }),
        ...(session.projectId && projects.loaded ? [projects.fetchAll({ suppressErrorToast: true })] : [])
      ])
    },
    async undoUserMessage(message) {
      const session = this.current
      if (!session || message?.role !== 'user' || this.historyActionPending) return null
      this.historyActionPending = true
      try {
        let files
        try {
          files = await downloadMessageAttachmentFiles(message)
        } catch (error) {
          notifyError(error, 'Die Anhänge konnten nicht wiederhergestellt werden.')
          throw error
        }
        const replacementError = this.attachmentReplacementError(
          files,
          session.id,
          session.id
        )
        if (replacementError) {
          notifyError({}, replacementError)
          throw new Error(replacementError)
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
          if (!data && ambiguous && !mutationKnownUnchanged) {
            this.drafts.sessionMessages[session.id] = message.content
            this.replaceAttachmentFiles(files, session.id, message.attachments)
            this.persistDrafts()
          }
          if (!data) {
            notifyError(error)
            throw error
          }
        }
        const stillSelected = this.selectedSessionId === session.id && this.current?.id === session.id
        const updatedSession = recoveredSession || sessionBeforeUserMessage(
          stillSelected ? this.current : session,
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
        this.drafts.sessionMessages[session.id] = data.draft.content
        this.replaceAttachmentFiles(files, session.id, message.attachments)
        if (stillSelected && !recoveredSession) {
          void this.refreshSession(session.id).catch(() => {})
        }
        this.persistDrafts()
        void useApprovalStore().fetchPending()
        void useQuestionStore().fetchPending().catch(() => {})
        return data
      } finally {
        this.historyActionPending = false
      }
    },
    async retryFailedMessage(message) {
      const session = this.current
      if (!session || message?.kind !== 'runtime_error' || this.historyActionPending) return null
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
        const stillSelected = this.selectedSessionId === session.id && this.current?.id === session.id
        const updatedSession = recoveredSession || sessionBeforeFailedResponse(
          stillSelected ? this.current : session,
          message.id,
          activeRun
        )
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
