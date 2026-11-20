import { markRaw, toRaw } from 'vue'
import { api } from 'src/boot/axios'
import { controlCenterRequestState, useControlCenterStore } from 'src/stores/control-center'
import { useWorkspaceStore } from 'src/stores/workspace'
import { buildAttachmentFormData, releaseAttachmentDrafts } from 'src/features/chat/attachments'
import { newSessionQueueKey, queueKeyForEntry, sanitizedSessionOptions, sessionCreatePayload, sessionScopeKey, sortSessions } from 'src/features/chat/state'
import { errorDescriptor } from 'src/utils/api-errors'
import { compareChatOutboxEntries, deleteChatOutboxEntry, deleteChatOutboxEntries, pauseChatOutboxEntries, saveChatOutboxEntry } from 'src/features/chat/outbox-storage'
import { notifyError } from 'src/utils/notifications'
import { randomUuid } from 'src/utils/random-id'
import { OUTBOX_RETRY_MS } from 'src/features/chat/model'

export const outboxActions = {
    async send(content, { preserveDraft = false, interrupt = false, activeFile } = {}) {
      if (this.loading || this.outboxSaving || this.historyActionPending || this.queueUndoId) return null
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
        entry = await this.queueMessage(content, { preserveDraft, interrupt: interruptRun, activeFile })
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
    async queueMessage(content, { preserveDraft = false, interrupt = false, activeFile } = {}) {
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
        activeFile: activeFile === undefined ? useWorkspaceStore().activeFileSnapshot(
          this.activeContext?.preferredExecutionAgentId
        ) : activeFile,
        queuedAt: new Date(this.activeQueuedMessages.reduce(
          (latest, item) => Math.max(latest, Date.parse(item.queuedAt) + 1), Date.now()
        )).toISOString(),
        ...(interrupt ? { interrupt: true } : {}),
        ...(interrupt && this.runId && this.runSessionId === sessionId
          ? { continuationRunId: this.runId } : {}),
        paused: false
      }
      if (sessionId && entry.agentProfileId) {
        this.drafts.sessionAgentProfiles[sessionId] = entry.agentProfileId
        this.persistDrafts()
      }
      this.pendingOutboxEntry = { ...entry, preserveDraft }
      const save = markRaw(saveChatOutboxEntry(toRaw(entry)))
      this.pendingOutboxSave = save
      try {
        await save
      } catch (error) {
        notifyError({}, 'Die Nachricht konnte nicht auf diesem Gerät vorgemerkt werden.')
        return null
      } finally {
        if (this.pendingOutboxEntry?.id === entry.id) this.pendingOutboxEntry = null
        if (this.pendingOutboxSave === save) this.pendingOutboxSave = null
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
    async removeQueuedMessage(id, key = this.activeQueueKey, { undoEntry, stillOwned = () => true } = {}) {
      if (id === this.queueSendingEntryId) return null
      const queue = this.queuedMessages[key] || []
      const entry = queue.find((item) => item.id === id) || undoEntry
      if (!entry) return null
      const paused = entry.paused
      const checkServer = entry.serverQueued || entry.deliveryUnconfirmed
      const ids = [...new Set([id, ...queue.filter((item) => item.afterRunId === id).map((item) => item.id)])]
      // Keep the local copy, but prevent a reload from retrying it during removal.
      entry.paused = true
      try {
        if (checkServer) {
          await pauseChatOutboxEntries([id])
          if (!stillOwned()) return null
          await api.delete(`/sessions/${entry.sessionId}/queue/${entry.id}`, {
            suppressErrorToast: true,
            ...(entry.deliveryUnconfirmed && !entry.serverQueued ? { params: { require_present: true } } : {})
          })
          if (stillOwned()) this.serverQueueVersion += 1
        }
        if (!stillOwned()) return null
        await deleteChatOutboxEntries(ids)
      } catch (error) {
        if (!stillOwned()) return null
        entry.paused = Boolean(entry.deliveryUnconfirmed || paused)
        if (entry.paused && this.queuedMessages[key]?.[0]?.id === id) this.queuedSendPausedSessions[key] = true
        if (undoEntry) throw error
        notifyError(error)
        return null
      }
      if (!stillOwned()) return null
      entry.paused = paused
      this.queuedMessages[key] = (this.queuedMessages[key] || queue).filter((item) => item.id !== id && item.afterRunId !== id)
      const remaining = this.queuedMessages[key]
      releaseAttachmentDrafts(entry.attachments)
      if (!remaining.length) {
        delete this.queuedMessages[key]
        delete this.queuedSendPausedSessions[key]
      } else if (remaining[0].paused) {
        this.queuedSendPausedSessions[key] = true
      } else {
        delete this.queuedSendPausedSessions[key]
        this.scheduleQueueDrain(0)
      }
      return entry
    },
    async restoreQueuedMessage(id, key = this.activeQueueKey, { confirmDraftUndo, confirmSentUndo } = {}) {
      if (this.queueUndoId || this.historyActionPending) return null
      const owner = this.draftOwnerUserId
      const version = this.outboxLoadVersion
      const stillOwned = () => owner === this.draftOwnerUserId && version === this.outboxLoadVersion
      const sending = id === this.queueSendingEntryId
      const saving = id === this.pendingOutboxEntry?.id ? this.pendingOutboxSave : null
      let entry = this.queuedMessages[key]?.find((item) => item.id === id)
      if (!entry && !saving) return null
      this.queueUndoId = id
      try {
        if (saving) {
          await saving.catch(() => {})
          if (!stillOwned()) return null
          entry = this.queuedMessages[key]?.find((item) => item.id === id)
        }
        // An aborted HTTP request may already have committed on the server.
        if (sending) await this.queueDrainPromise
        if (!entry || !stillOwned()) return null
        const sessionId = entry.sessionId || null
        const restoreSent = async (messageId, sentSessionId = sessionId) => {
          if (!stillOwned()) return null
          const session = this.current?.id === sentSessionId && this.current.messages.some((message) => message.id === messageId)
            ? this.current : await this.fetchSession(sentSessionId)
          const message = session.messages.find((item) => item.id === messageId)
            || (await api.get(`/sessions/${sentSessionId}/messages/${messageId}`, { suppressErrorToast: true })).data
          if (!stillOwned()) return null
          if (confirmSentUndo && !await confirmSentUndo(session)) return null
          if (!stillOwned()) return null
          const files = entry.attachments.length === (message.attachments || []).length
            && entry.attachments.every((item) => item.file instanceof Blob)
            ? entry.attachments.map((item) => item.file) : undefined
          const history = session.messages.includes(message) ? session : { ...session, messages: [message] }
          const result = await this.undoUserMessage(message, { session: history, files, outboxId: id }).catch(() => null)
          if (result && stillOwned()) {
            this.serverQueueVersion += 1
            await deleteChatOutboxEntry(id)
            if (!stillOwned()) return null
            const entryKey = queueKeyForEntry(entry)
            const queue = this.queuedMessages[entryKey]?.filter((item) => item.id !== id)
            if (queue?.length) this.queuedMessages[entryKey] = queue
            else delete this.queuedMessages[entryKey]
            if (!sessionId && !this.current && this.activeQueueKey === key) {
              this.createdSessionResult = { created: true, sessionId: sentSessionId, projectId: session.projectId }
            }
            void this.refreshServerQueue().catch(() => {})
          }
          return result
        }
        if (entry.sentMessageId) return await restoreSent(entry.sentMessageId)
        if (entry.deliveryUnconfirmed && (!sessionId || entry.continuationRunId)) {
          if (sessionId) return await restoreSent(id)
          const { data } = await api.get(`/sessions/message-submissions/${id}`, { suppressErrorToast: true })
          return await restoreSent(data.messageId, data.sessionId)
        }
        if (confirmDraftUndo && !await confirmDraftUndo()) return null
        if (!stillOwned()) return null
        let attachments = entry.attachments
        try {
          if (entry.serverQueued) {
            attachments = await Promise.all(attachments.map(async (attachment, index) => {
              if (attachment.file instanceof Blob) return attachment
              const { data } = await api.get(`/sessions/${sessionId}/queue/${id}/attachments/${index}`, {
                responseType: 'blob', suppressErrorToast: true
              })
              return { ...attachment, file: markRaw(new File([data], attachment.name, { type: attachment.mimeType })) }
            }))
          }
          if (!stillOwned()) return null
          if (!await this.removeQueuedMessage(id, sessionId || key, { undoEntry: entry, stillOwned })) return null
        } catch (error) {
          const descriptor = errorDescriptor(error)
          if (descriptor?.code !== 'session.runAlreadyActive' || !descriptor.params?.messageId) throw error
          return await restoreSent(descriptor.params.messageId)
        }
        if (!stillOwned()) return null
        if (sessionId) this.drafts.sessionMessages[sessionId] = entry.content
        else {
          const draft = this.drafts.newSessions[sessionScopeKey(entry.projectId)]
          if (draft) draft.message = entry.content
        }
        if (sessionId && entry.agentProfileId) {
          this.profileSelectionVersions[sessionId] = (this.profileSelectionVersions[sessionId] || 0) + 1
          this.drafts.sessionAgentProfiles[sessionId] = entry.agentProfileId
        }
        this.replaceAttachmentFiles(attachments.map((attachment) => attachment.file), sessionId, attachments, entry.projectId)
        this.persistDrafts()
        return entry
      } catch (error) {
        if (stillOwned()) {
          if (entry?.deliveryUnconfirmed && errorDescriptor(error)?.code === 'session.messageNotFound') {
            notifyError({}, 'Der Versand konnte noch nicht geklärt werden. Die Nachricht bleibt im Postausgang pausiert.')
          } else notifyError(error)
        }
        return null
      } finally {
        if (this.queueUndoId === id) {
          this.queueUndoId = null
          this.scheduleQueueDrain(0)
        }
      }
    },
    async clearQueuedMessages(key = this.activeQueueKey) {
      const entries = this.queuedMessages[key] || []
      if (!entries.length) return
      for (const entry of [...entries]) {
        if (entry.serverQueued && !await this.removeQueuedMessage(entry.id, key)) return
      }
      await deleteChatOutboxEntries(entries.filter((entry) => !entry.serverQueued).map((entry) => entry.id))
      releaseAttachmentDrafts(entries.flatMap((entry) => entry.attachments))
      delete this.queuedMessages[key]
      delete this.queuedSendPausedSessions[key]
    },
    async resumeQueuedSend(key = this.activeQueueKey) {
      if (!key || this.queueUndoId) return
      const entry = this.queuedMessages[key]?.[0]
      if (entry?.paused) {
        if (entry.serverQueued) {
          await api.patch(`/sessions/${entry.sessionId}/queue/${entry.id}`, { paused: false })
          await this.refreshServerQueue()
          return
        }
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
          Object.entries(this.queuedMessages).some(([key, queue]) => {
            const entry = queue.find((item) => !item.serverQueued)
            return entry && !entry.paused && (entry.sessionId || key === this.activeQueueKey)
          })
        ) this.scheduleQueueDrain(this.historyActionPending || this.queueUndoId || this.sessionSettingsPending || this.stopping ? OUTBOX_RETRY_MS : 0)
      }
    },
    async drainNextOutboxEntry() {
      this.cancelQueueDrain()
      if (this.historyActionPending || this.queueUndoId || this.sessionSettingsPending || this.stopping) return null
      const entry = Object.entries(this.queuedMessages)
        .filter(([key]) => key === this.activeQueueKey || !key.startsWith('new:'))
        .map(([, queue]) => queue.find((item) => !item.serverQueued)).filter((item) => item && !item.paused)
        .sort(compareChatOutboxEntries)[0]
      if (!entry) return null
      return this.submitOutboxEntry(entry)
    },
    async refreshServerQueue() {
      const owner = this.draftOwnerUserId, version = this.outboxLoadVersion, requestVersion = ++this.serverQueueVersion
      if (!owner) return
      const { data } = await api.get('/sessions/message-queue', { suppressErrorToast: true })
      if (owner !== this.draftOwnerUserId || version !== this.outboxLoadVersion || requestVersion !== this.serverQueueVersion) return
      const previous = new Map(Object.values(this.queuedMessages).flat().map((entry) => [entry.id, entry]))
      const serverIds = new Set((data.items || []).map((entry) => entry.id))
      if ([...previous.values()].some((entry) => entry.serverQueued && entry.sessionId === this.current?.id && !serverIds.has(entry.id))) {
        await this.refreshSession(this.current.id)
      }
      if (owner !== this.draftOwnerUserId || version !== this.outboxLoadVersion || requestVersion !== this.serverQueueVersion) return
      const accepted = [...previous.values()].filter((entry) => entry.serverQueued || serverIds.has(entry.id))
      if (accepted.length) await deleteChatOutboxEntries(accepted.map((entry) => entry.id))
      if (owner !== this.draftOwnerUserId || version !== this.outboxLoadVersion || requestVersion !== this.serverQueueVersion) return
      for (const [key, queue] of Object.entries(this.queuedMessages)) {
        this.queuedMessages[key] = queue.filter((entry) => !entry.serverQueued && !serverIds.has(entry.id))
        if (!this.queuedMessages[key].length) delete this.queuedMessages[key]
      }
      for (const entry of data.items || []) {
        const old = previous.get(entry.id)
        entry.attachments = entry.attachments.map((attachment, index) => ({ ...attachment,
          ...(old?.attachments[index]?.file ? { file: old.attachments[index].file } : {}) }))
        if (!this.queuedMessages[entry.sessionId]) this.queuedMessages[entry.sessionId] = []
        this.queuedMessages[entry.sessionId].push(entry)
        this.queuedMessages[entry.sessionId].sort(compareChatOutboxEntries)
        if (entry.errorCode && entry.errorCode !== old?.errorCode) notifyError({ code: entry.errorCode })
      }
      this.queuedSendPausedSessions = Object.fromEntries(Object.entries(this.queuedMessages)
        .filter(([, queue]) => queue[0]?.paused).map(([key]) => [key, true]))
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
            ...(entry.agentProfileId ? { agent_profile_id: entry.agentProfileId } : {}),
            ...(entry.activeFile ? { active_file: entry.activeFile } : {})
          }
          const url = `/sessions/${entry.sessionId}/runs/${entry.continuationRunId}/messages`
          const { data } = await (entry.attachments.length
            ? api.post(`${url}/with-attachments`, buildAttachmentFormData(payload, entry.attachments), { suppressErrorToast: true })
            : api.post(url, payload, { suppressErrorToast: true }))
          entry.sentMessageId = data.message.id
          data.message.renderId = entry.id
          this.updateCachedSession(entry.sessionId, (session) => ({ ...session, lastUserMessageAt: data.message.createdAt }))
          if (this.current?.id === entry.sessionId && this.selectedSessionId === entry.sessionId) {
            this.current.lastUserMessageAt = data.message.createdAt
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
        } else {
          const payload = {
            content: entry.content,
            run_id: entry.id,
            enqueue: true,
            ...(entry.afterRunId ? { after_run_id: entry.afterRunId } : {}),
            ...(entry.agentProfileId ? { agent_profile_id: entry.agentProfileId } : {}),
            ...(entry.activeFile ? { active_file: entry.activeFile } : {})
          }
          const config = { suppressErrorToast: true }
          await (entry.attachments.length
            ? api.post(
                `/sessions/${entry.sessionId}/messages/with-attachments`,
                buildAttachmentFormData(payload, entry.attachments),
                config
              )
            : api.post(`/sessions/${entry.sessionId}/messages`, payload, config))
          entry.serverQueued = true
          this.serverQueueVersion += 1
          await deleteChatOutboxEntry(entry.id)
          await this.refreshServerQueue()
          return { created: false, sessionId: entry.sessionId }
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
        if (entry.serverQueued) return { created: false, sessionId: entry.sessionId }
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
          if (!error?.response || error.response.status >= 500) entry.deliveryUnconfirmed = true
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
        if (runId) userMessage.renderId = runId
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
          { ...data.session, lastUserMessageAt: data.session.lastUserMessageAt || userMessage.createdAt },
          ...list.items.filter((item) => item.id !== session.id)
        ])
        const recordings = this.getAudioDictation(null, projectId)
        if (recordings) this.setAudioDictation(recordings, session.id)
        const composerProfileId = stillSelected
          ? this.drafts.newSession.options.agentProfileId || options.agentProfileId
          : options.agentProfileId
        if (composerProfileId) this.drafts.sessionAgentProfiles[session.id] = composerProfileId
        this.resetNewDraft(projectId)
        if (queuedEntry) {
          const key = newSessionQueueKey(projectId)
          const queue = this.queuedMessages[key]
          for (const entry of queue) {
            entry.sessionId = session.id
            if (entry.id !== runId) await saveChatOutboxEntry(toRaw(entry))
          }
          this.queuedMessages[session.id] = [...(this.queuedMessages[session.id] || []), ...queue].sort(compareChatOutboxEntries)
          delete this.queuedMessages[key]
        }
        if (stillSelected) {
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
}
