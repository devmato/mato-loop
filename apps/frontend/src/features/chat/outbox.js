import { markRaw, toRaw } from 'vue'
import { api } from 'src/boot/axios'
import { controlCenterRequestState, useControlCenterStore } from 'src/stores/control-center'
import { useWorkspaceStore } from 'src/stores/workspace'
import { buildAttachmentFormData, releaseAttachmentDrafts } from 'src/features/chat/attachments'
import { newSessionQueueKey, queueKeyForEntry, sanitizedSessionOptions, sessionCreatePayload, sessionScopeKey, sortSessions } from 'src/features/chat/state'
import { errorDescriptor } from 'src/utils/api-errors'
import { compareChatOutboxEntries, deleteChatOutboxEntry, deleteChatOutboxEntries, saveChatOutboxEntry } from 'src/utils/chat-outbox'
import { notifyError } from 'src/utils/notifications'
import { randomUuid } from 'src/utils/random-id'
import { OUTBOX_RETRY_MS } from 'src/features/chat/model'

export const outboxActions = {
    async send(content, { preserveDraft = false, interrupt = false } = {}) {
      if (this.outboxSaving || this.historyActionPending) return null
      const sessionId = this.current?.id
      const ownerUserId = this.draftOwnerUserId
      const draft = this.drafts.newSession
      this.outboxSaving = true
      let entry
      let submitImmediately = false
      try {
        while (this.sessionSettingsPromise) await this.sessionSettingsPromise
        if (this.draftOwnerUserId !== ownerUserId || this.current?.id !== sessionId
          || (!sessionId && this.drafts.newSession !== draft)) return null
        if (!this.current && this.draftPreviewRequests[sessionScopeKey(this.sessionsProjectId)]) {
          await this.waitForDraftDefaults()
          if (this.current || this.drafts.newSession !== draft) return null
        }
        if (this.historyActionPending || this.sessionSettingsPending) return null
        if (/^\/fork(?:\s|$)/iu.test(String(content || '').trim())) {
          if (String(content).trim().toLowerCase() !== '/fork' || (!preserveDraft && this.activeAttachmentDraft.length)) {
            notifyError({}, '/fork wird ohne Argumente oder neue Anhänge ausgeführt.')
            return null
          }
          return await this.forkSession()
        }
        const interruptRun = interrupt && this.running && Boolean(this.current)
          && (!this.runSessionId || this.runSessionId === this.current.id)
        submitImmediately = !this.running
          && !this.stopping
          && !this.runId
          && !this.activeQueuedMessages.length
          && !this.queueDrainPromise
          && !this.messageQueueActive
        if (submitImmediately) {
          this.running = true
          this.setActivity('submitting', { startedAt: new Date().toISOString() })
        }
        if (this.outboxLoadPromise) await this.outboxLoadPromise
        entry = await this.queueMessage(content, { preserveDraft, interrupt: interruptRun })
      } finally {
        this.outboxSaving = false
      }
      if (!entry) {
        if (submitImmediately) {
          this.running = false
          this.stopping = false
          this.cancelRequested = false
          this.clearActivity()
        }
        return null
      }
      if (submitImmediately) {
        this.running = false
      }
      return this.drainOutbox()
    },
    async queueMessage(content, { preserveDraft = false, interrupt = false } = {}) {
      const ownerUserId = this.draftOwnerUserId
      const loadVersion = this.outboxLoadVersion
      if (!ownerUserId) return null
      const text = String(content || '').trim()
      const attachments = preserveDraft ? [] : [...this.activeAttachmentDraft]
      if (!text && !attachments.length) return null
      const sessionId = this.current?.id || null
      const entry = {
        id: randomUuid(),
        ownerUserId,
        sessionId,
        projectId: this.current?.projectId || this.sessionsProjectId || null,
        content: text,
        attachments: attachments.map((attachment) => ({
          id: attachment.id,
          file: attachment.file,
          name: attachment.file.name || 'datei',
          kind: attachment.kind,
          mimeType: attachment.mimeType,
          voiceMessage: attachment.voiceMessage === true
        })),
        options: sessionId ? null : JSON.parse(JSON.stringify(
          sanitizedSessionOptions(this.drafts.newSession.options)
        )),
        agentProfileId: sessionId ? this.activeAgentProfileId || null : null,
        controlCenter: sessionId ? null : controlCenterRequestState(useControlCenterStore()),
        activeFile: useWorkspaceStore().activeFileSnapshot(
          this.activeContext?.preferredExecutionAgentId
        ),
        queuedAt: new Date(this.activeQueuedMessages.reduce(
          (latest, item) => Math.max(latest, Date.parse(item.queuedAt) + 1), Date.now()
        )).toISOString(),
        ...(interrupt ? { interrupt: true } : {}),
        ...(interrupt && this.runId && this.runSessionId === sessionId
          ? { continuationRunId: this.runId } : {}),
        paused: false
      }
      try {
        await saveChatOutboxEntry(toRaw(entry))
      } catch (error) {
        notifyError({}, 'Die Nachricht konnte nicht auf diesem Gerät vorgemerkt werden.')
        return null
      }
      if (ownerUserId !== this.draftOwnerUserId || loadVersion !== this.outboxLoadVersion) {
        await deleteChatOutboxEntry(entry.id).catch(() => {})
        return null
      }
      const key = queueKeyForEntry(entry)
      if (!this.queuedMessages[key]) this.queuedMessages[key] = []
      this.queuedMessages[key].push(entry)
      this.queuedMessages[key].sort(compareChatOutboxEntries)
      if (!this.queuedMessages[key][0].paused) delete this.queuedSendPausedSessions[key]
      if (!preserveDraft) {
        this.clearAttachmentDraft(sessionId)
        if (sessionId) delete this.drafts.sessionMessages[sessionId]
        else this.drafts.newSession.message = ''
        this.persistDrafts()
      }
      return entry
    },
    async removeQueuedMessage(id, key = this.activeQueueKey) {
      if (id === this.queueSendingEntryId) return null
      const queue = key ? this.queuedMessages[key] : null
      const index = queue ? queue.findIndex((entry) => entry.id === id) : -1
      if (index < 0) return null
      const entry = queue[index]
      try {
        await deleteChatOutboxEntry(entry.id)
      } catch {
        notifyError({}, 'Die vorgemerkte Nachricht konnte nicht entfernt werden.')
        return null
      }
      queue.splice(index, 1)
      releaseAttachmentDrafts(entry.attachments)
      if (!queue.length) {
        delete this.queuedMessages[key]
        delete this.queuedSendPausedSessions[key]
      } else if (queue[0].paused) {
        this.queuedSendPausedSessions[key] = true
      } else {
        delete this.queuedSendPausedSessions[key]
      }
      return entry
    },
    async restoreQueuedMessage(id, key = this.activeQueueKey) {
      const entry = await this.removeQueuedMessage(id, key)
      if (!entry) return null
      const sessionId = entry.sessionId || null
      if (sessionId) this.drafts.sessionMessages[sessionId] = entry.content
      else this.drafts.newSession.message = entry.content
      if (sessionId && entry.agentProfileId) this.drafts.sessionAgentProfiles[sessionId] = entry.agentProfileId
      this.replaceAttachmentFiles(entry.attachments.map((attachment) => attachment.file), sessionId, entry.attachments)
      this.persistDrafts()
      return entry
    },
    async clearQueuedMessages(key = this.activeQueueKey) {
      const entries = this.queuedMessages[key] || []
      if (!entries.length) return
      await deleteChatOutboxEntries(entries.map((entry) => entry.id))
      releaseAttachmentDrafts(entries.flatMap((entry) => entry.attachments))
      delete this.queuedMessages[key]
      delete this.queuedSendPausedSessions[key]
    },
    async resumeQueuedSend(key = this.activeQueueKey) {
      if (!key) return
      const entry = this.queuedMessages[key]?.[0]
      if (entry?.paused) {
        entry.paused = false
        try {
          await saveChatOutboxEntry(toRaw(entry))
        } catch {
          entry.paused = true
          notifyError({}, 'Der Versand konnte nicht wieder aufgenommen werden.')
          return
        }
      }
      delete this.queuedSendPausedSessions[key]
      this.scheduleQueueDrain(0)
    },
    cancelQueueDrain() {
      if (this.queueDrainTimer !== null) clearTimeout(this.queueDrainTimer)
      this.queueDrainTimer = null
    },
    scheduleQueueDrain(delay = OUTBOX_RETRY_MS) {
      if (this.queueDrainTimer !== null) return
      this.queueDrainTimer = setTimeout(() => {
        this.queueDrainTimer = null
        void this.drainOutbox()
      }, delay)
    },
    async drainOutbox() {
      if (!this.outboxLoaded) return null
      if (this.queueDrainPromise) return this.queueDrainPromise
      const request = this.drainNextOutboxEntry()
      this.queueDrainPromise = markRaw(request)
      try {
        return await request
      } finally {
        if (this.queueDrainPromise === request) this.queueDrainPromise = null
        if (
          this.queuedSendReady
          || Object.values(this.queuedMessages).some((queue) => (
            queue[0]?.sessionId && queue[0].sessionId !== this.selectedSessionId
              && !this.queuedSendPausedSessions[queueKeyForEntry(queue[0])]
          ))
        ) this.scheduleQueueDrain(0)
      }
    },
    async drainNextOutboxEntry() {
      this.cancelQueueDrain()
      let entry = this.queuedSendReady ? this.activeQueuedMessages[0] : null
      if (!entry) {
        const backgroundEntries = Object.values(this.queuedMessages)
          .filter((queue) => queue[0]?.sessionId && queue[0].sessionId !== this.selectedSessionId)
          .map((queue) => queue[0])
          .filter((item) => !this.queuedSendPausedSessions[queueKeyForEntry(item)])
          .sort((left, right) => left.queuedAt.localeCompare(right.queuedAt))
        for (const candidate of backgroundEntries) {
          try {
            const { data } = await api.get(`/sessions/${candidate.sessionId}`, {
              suppressErrorToast: true
            })
            if (!data.activeRun || candidate.continuationRunId === data.activeRun.id) {
              entry = candidate
              break
            }
          } catch (error) {
            const key = queueKeyForEntry(candidate)
            candidate.paused = true
            try {
              await saveChatOutboxEntry(toRaw(candidate))
            } catch {
              notifyError({}, 'Der Pausenstatus konnte nicht gespeichert werden.')
            }
            this.queuedSendPausedSessions[key] = true
            notifyError(error)
          }
        }
      }
      if (!entry) {
        if (Object.values(this.queuedMessages).some((queue) => (
          queue[0]?.sessionId && !this.queuedSendPausedSessions[queueKeyForEntry(queue[0])]
        ))) this.scheduleQueueDrain()
        return null
      }
      return this.submitOutboxEntry(entry)
    },
    async submitOutboxEntry(entry) {
      this.queueSendingEntryId = entry.id
      try {
        let result
        if (entry.sentMessageId) {
          result = { created: false, sessionId: entry.sessionId }
        } else if (!entry.sessionId) {
          result = await this.startSession(entry.content, entry.attachments, {
            options: entry.options,
            projectId: entry.projectId,
            controlCenter: entry.controlCenter,
            activeFile: entry.activeFile,
            runId: entry.id,
            suppressErrorToast: true
          })
          if (result?.created) this.createdSessionResult = result
        } else if (entry.continuationRunId) {
          const payload = {
            content: entry.content,
            message_id: entry.id,
            ...(entry.activeFile ? { active_file: entry.activeFile } : {})
          }
          const url = `/sessions/${entry.sessionId}/runs/${entry.continuationRunId}/messages`
          const { data } = await (entry.attachments.length
            ? api.post(`${url}/with-attachments`, buildAttachmentFormData(payload, entry.attachments), { suppressErrorToast: true })
            : api.post(url, payload, { suppressErrorToast: true }))
          if (this.current?.id === entry.sessionId && this.selectedSessionId === entry.sessionId) {
            if (!this.current.messages.some((message) => message.id === data.message.id)) {
              const later = this.current.messages.findIndex((message) => (
                message.role === 'assistant' && message.runId === entry.continuationRunId
                && message.createdAt && data.message.createdAt && message.createdAt > data.message.createdAt
              ))
              if (later < 0) this.current.messages.push(data.message)
              else this.current.messages.splice(later, 0, data.message)
            }
          } else {
            void this.refreshSessions(null, entry.projectId).catch(() => {})
          }
          result = { created: false, sessionId: entry.sessionId }
        } else if (this.current?.id === entry.sessionId && this.selectedSessionId === entry.sessionId) {
          result = await this.sendToCurrent(entry.content, entry.attachments, {
            clearDraft: false,
            activeFile: entry.activeFile,
            agentProfileId: entry.agentProfileId,
            runId: entry.id,
            suppressErrorToast: true
          })
        } else {
          const payload = {
            content: entry.content,
            run_id: entry.id,
            ...(entry.agentProfileId ? { agent_profile_id: entry.agentProfileId } : {}),
            ...(entry.activeFile ? { active_file: entry.activeFile } : {})
          }
          const config = { suppressErrorToast: true }
          const { data } = await (entry.attachments.length
            ? api.post(
                `/sessions/${entry.sessionId}/messages/with-attachments`,
                buildAttachmentFormData(payload, entry.attachments),
                config
              )
            : api.post(`/sessions/${entry.sessionId}/messages`, payload, config))
          entry.sentMessageId = data.message?.id
          if (data?.runId && this.current?.id === entry.sessionId && this.selectedSessionId === entry.sessionId) {
            if (data.agentProfileId) this.current.agentProfileId = data.agentProfileId
            if (data.message && !this.current.messages.some((message) => message.id === data.message.id)) {
              this.current.messages.push(data.message)
            }
            if (this.runId !== data.runId || !this.eventSource) {
              this.watchRun(entry.sessionId, data.runId, { status: 'queued', ...data })
            }
          }
          void this.refreshSessions(null, entry.projectId).catch(() => {})
          result = { created: false, sessionId: entry.sessionId }
        }
        if (
          entry.sessionId
          && !entry.continuationRunId
          && this.queuedMessages[queueKeyForEntry(entry)]?.length === 1
          && this.drafts.sessionAgentProfiles[entry.sessionId] === entry.agentProfileId
        ) {
          delete this.drafts.sessionAgentProfiles[entry.sessionId]
          this.persistDrafts()
        }
        const key = queueKeyForEntry(entry)
        await deleteChatOutboxEntry(entry.id)
        const queue = this.queuedMessages[key] || []
        this.queuedMessages[key] = queue.filter((item) => item.id !== entry.id)
        if (!this.queuedMessages[key].length) delete this.queuedMessages[key]
        if (this.queuedMessages[key]?.[0]?.paused) this.queuedSendPausedSessions[key] = true
        else delete this.queuedSendPausedSessions[key]
        releaseAttachmentDrafts(entry.attachments)
        return result
      } catch (error) {
        const key = queueKeyForEntry(entry)
        const stillQueued = this.queuedMessages[key]?.some((item) => item.id === entry.id)
        if (!stillQueued) return null
        if (entry.continuationRunId && errorDescriptor(error)?.code === 'session.runInactive') {
          delete entry.continuationRunId
          try {
            await saveChatOutboxEntry(toRaw(entry))
          } catch {
            notifyError({}, 'Die vorgemerkte Nachricht konnte nicht gespeichert werden.')
          }
          this.scheduleQueueDrain(0)
        } else if (errorDescriptor(error)?.code === 'session.runAlreadyActive') {
          this.scheduleQueueDrain()
        } else {
          entry.paused = true
          try {
            await saveChatOutboxEntry(toRaw(entry))
          } catch {
            notifyError({}, 'Der Pausenstatus konnte nicht gespeichert werden.')
          }
          this.queuedSendPausedSessions[key] = true
          notifyError(error)
        }
        return null
      } finally {
        if (this.queueSendingEntryId === entry.id) this.queueSendingEntryId = null
      }
    },
    async stop() {
      if (!this.running || this.stopping) return
      this.cancelRequested = true
      this.running = false
      this.stopping = false
      void this.cancelActiveRun()
    },
    async cancelActiveRun() {
      if (!this.cancelRequested || !this.runSessionId || !this.runId) return
      const sessionId = this.runSessionId
      const runId = this.runId
      try {
        const url = `/sessions/${sessionId}/runs/${runId}/cancel`
        await api.post(url)
      } catch {
        if (this.runSessionId === sessionId && this.runId === runId) {
          this.cancelRequested = false
          this.stopping = false
          this.running = true
        }
      }
    },
    async startSession(
      content,
      attachments = [],
      {
        options: requestedOptions = null,
        projectId: requestedProjectId,
        controlCenter: requestedControlCenter = null,
        activeFile: requestedActiveFile,
        runId = null,
        suppressErrorToast = false
      } = {}
    ) {
      const sessionLoadVersion = this.sessionLoadVersion
      const selectedSessionId = this.selectedSessionId
      const projectId = requestedProjectId === undefined ? this.sessionsProjectId : requestedProjectId
      this.running = !this.cancelRequested
      this.streamContent = ''
      this.streamResearchSources = []
      this.streamReasoning = null
      this.streamContextSummary = null
      this.streamModelName = null
      this.processOutputs = {}
      this.completedToolCallId = null
      this.setActivity('submitting', { startedAt: new Date().toISOString() })
      const options = sanitizedSessionOptions(requestedOptions || this.drafts.newSession.options)
      try {
        const activeFile = requestedActiveFile === undefined
          ? useWorkspaceStore().activeFileSnapshot(
              this.activeContext?.preferredExecutionAgentId
            )
          : requestedActiveFile
        const payload = {
          content,
          ...(runId ? { run_id: runId } : {}),
          ...sessionCreatePayload(
            options, projectId,
            requestedControlCenter || controlCenterRequestState(useControlCenterStore())
          ),
          ...(activeFile ? { active_file: activeFile } : {})
        }
        const config = suppressErrorToast ? [{ suppressErrorToast: true }] : []
        const { data } = attachments.length
          ? await api.post('/sessions/with-attachments', buildAttachmentFormData(payload, attachments), ...config)
          : await api.post('/sessions', payload, ...config)
        const userMessage = data.message || { id: `local-${Date.now()}`, role: 'user', content }
        const queuedEntry = this.queuedMessages[newSessionQueueKey(projectId)]?.find((entry) => entry.id === runId)
        if (queuedEntry) queuedEntry.sentMessageId = userMessage.id
        const session = {
          ...data.session,
          userRequestCount: Number.isInteger(data.session.userRequestCount)
            ? Math.max(data.session.userRequestCount, 1)
            : 1,
          messages: [userMessage]
        }
        const wasStillSelected = this.sessionLoadVersion === sessionLoadVersion
          && this.selectedSessionId === selectedSessionId
          && !this.current
        if (wasStillSelected) {
          const controlCenter = useControlCenterStore()
          try {
            const reconciled = await controlCenter.attachCreatedSession(
              session.id,
              session.controlCenter
            )
            if (reconciled) session.controlCenter = reconciled
          } catch {
            if (
              this.sessionLoadVersion === sessionLoadVersion
              && this.selectedSessionId === selectedSessionId
              && !this.current
            ) controlCenter.syncSession(session)
          }
        }
        const stillSelected = wasStillSelected
          && this.sessionLoadVersion === sessionLoadVersion
          && this.selectedSessionId === selectedSessionId
          && !this.current
        const list = this.ensureSessionList(data.session.projectId)
        if (list.loadRequest) {
          try {
            await list.loadRequest
          } catch {
            // The created session remains canonical even if the independent list load failed.
          }
        }
        list.version += 1
        list.items = sortSessions([
          data.session,
          ...list.items.filter((item) => item.id !== session.id)
        ])
        const recordings = this.getAudioDictation(null, projectId)
        if (recordings) this.setAudioDictation(recordings, session.id)
        this.resetNewDraft(projectId)
        if (stillSelected) {
          if (queuedEntry) {
            const key = newSessionQueueKey(projectId)
            const queue = this.queuedMessages[key]
            for (const entry of queue) entry.sessionId = session.id
            this.queuedMessages[session.id] = queue
            delete this.queuedMessages[key]
          }
          this.current = session
          this.selectedSessionId = session.id
          this.watchRun(session.id, data.runId, {
            status: 'queued',
            modelName: data.modelName,
            agentProfileId: data.agentProfileId,
            activeDurationMs: data.activeDurationMs,
            activeDurationRunning: data.activeDurationRunning,
            activeTimingRevision: data.activeTimingRevision
          })
          await this.cancelActiveRun()
        }
        return { created: stillSelected, sessionId: session.id }
      } catch (error) {
        if (
          this.sessionLoadVersion === sessionLoadVersion
          && this.selectedSessionId === selectedSessionId
        ) {
          this.running = false
          this.stopping = false
          this.cancelRequested = false
          this.clearActivity()
        }
        throw error
      }
    },
    async sendToCurrent(
      content,
      attachments = [],
      { clearDraft = true, activeFile: requestedActiveFile, agentProfileId = null, runId = null, suppressErrorToast = false } = {}
    ) {
      const session = this.current
      const sessionLoadVersion = this.sessionLoadVersion
      this.running = !this.cancelRequested
      this.streamContent = ''
      this.streamResearchSources = []
      this.streamReasoning = null
      this.streamContextSummary = null
      this.streamModelName = null
      this.processOutputs = {}
      this.completedToolCallId = null
      this.setActivity('submitting', { startedAt: new Date().toISOString() })
      try {
        const activeFile = requestedActiveFile === undefined
          ? useWorkspaceStore().activeFileSnapshot(
              this.activeContext?.preferredExecutionAgentId
            )
          : requestedActiveFile
        const payload = {
          content,
          ...(runId ? { run_id: runId } : {}),
          ...(agentProfileId ? { agent_profile_id: agentProfileId } : {}),
          ...(activeFile ? { active_file: activeFile } : {})
        }
        const config = suppressErrorToast ? [{ suppressErrorToast: true }] : []
        const { data } = attachments.length
          ? await api.post(
              `/sessions/${session.id}/messages/with-attachments`,
              buildAttachmentFormData(payload, attachments),
              ...config
            )
          : await api.post(`/sessions/${session.id}/messages`, payload, ...config)
        const queuedEntry = this.queuedMessages[session.id]?.find((entry) => entry.id === runId)
        if (queuedEntry) queuedEntry.sentMessageId = data.message?.id
        if (data.message) {
          const targetSession = this.selectedSessionId === session.id && this.current?.id === session.id
            ? this.current : session
          if (data.agentProfileId) targetSession.agentProfileId = data.agentProfileId
          const canonicalIndex = targetSession.messages.findIndex((message) => message.id === data.message.id)
          if (canonicalIndex >= 0) {
            targetSession.messages[canonicalIndex] = data.message
          } else {
            targetSession.messages.push(data.message)
            if (data.message.role === 'user' && Number.isInteger(targetSession.userRequestCount)) {
              targetSession.userRequestCount += 1
            }
          }
          if (data.message.createdAt) {
            targetSession.lastMessageAt = data.message.createdAt
            this.updateSessionSummary(targetSession)
          }
        }
        if (clearDraft) {
          this.clearAttachmentDraft(session.id)
          delete this.drafts.sessionMessages[session.id]
          this.persistDrafts()
        }
        if (
          this.selectedSessionId === session.id
          && this.current?.id === session.id
        ) {
          if (this.runId !== data.runId || !this.eventSource) {
            this.watchRun(session.id, data.runId, {
              status: 'queued',
              modelName: data.modelName,
              agentProfileId: data.agentProfileId,
              activeDurationMs: data.activeDurationMs,
              activeDurationRunning: data.activeDurationRunning,
              activeTimingRevision: data.activeTimingRevision
            })
          }
          await this.cancelActiveRun()
        } else {
          void this.refreshSessions().catch(() => {})
        }
        return { created: false, sessionId: session.id }
      } catch (error) {
        if (
          this.sessionLoadVersion === sessionLoadVersion
          && this.selectedSessionId === session.id
          && this.current?.id === session.id
        ) {
          this.running = false
          this.stopping = false
          this.cancelRequested = false
          this.clearActivity()
        }
        throw error
      }
    },
}
