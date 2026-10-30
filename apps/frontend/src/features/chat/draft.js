import { markRaw } from 'vue'
import { api } from 'src/boot/axios'
import { useInstanceLimitsStore } from 'src/stores/instance-limits'
import { attachmentKind, attachmentPreviewUrl, MAX_ATTACHMENT_DRAFT_BYTES, normalizedMimeType, releaseAttachmentDrafts } from 'src/features/chat/attachments'
import { clearPersistedDrafts, defaultSessionOptions, emptyActivity, emptyDrafts, groupedOutbox, loadDrafts, newSessionQueueKey, normalizedNewSessionDraft, persistDrafts, sanitizedSessionOptions, sessionScopeKey } from 'src/features/chat/state'
import { mergeAgentActivity } from 'src/utils/agent-activity'
import { clearChatOutbox, loadChatOutbox } from 'src/utils/chat-outbox'

export const draftActions = {
    setActivity(phase, details = {}) {
      this.activity = mergeAgentActivity(this.activity, { ...details, phase })
    },
    setRunTiming(timing = {}) {
      this.activity = mergeAgentActivity(this.activity, { ...timing, phase: null })
    },
    clearActivity() {
      this.activity = emptyActivity(this.activity.revision + 1)
    },
    persistDrafts() {
      if (!this.draftOwnerUserId || typeof window === 'undefined') return
      try {
        this.drafts.newSessions[sessionScopeKey(this.sessionsProjectId)] = this.drafts.newSession
        persistDrafts(this.draftOwnerUserId, this.drafts)
      } catch {
        // Chat remains usable when local storage is blocked or full.
      }
    },
    setDraftOwner(userId) {
      const ownerUserId = typeof userId === 'string' && userId ? userId : null
      if (ownerUserId === this.draftOwnerUserId) return
      this.cancelQueueDrain()
      this.clearAllAttachmentDrafts()
      this.draftOwnerUserId = ownerUserId
      this.queuedMessages = {}
      this.queuedSendPausedSessions = {}
      this.outboxLoaded = !ownerUserId
      this.outboxLoadFailed = false
      const loadVersion = ++this.outboxLoadVersion
      this.outboxLoadPromise = null
      this.drafts = loadDrafts(ownerUserId)
      const scope = sessionScopeKey(this.sessionsProjectId)
      if (!this.drafts.newSessions[scope]) {
        this.drafts.newSessions[scope] = normalizedNewSessionDraft(null)
      }
      this.drafts.newSession = this.drafts.newSessions[scope]
      if (!this.attachmentDrafts.newSessions[scope]) this.attachmentDrafts.newSessions[scope] = []
      this.attachmentDrafts.newSession = this.attachmentDrafts.newSessions[scope]
      this.audioDictations.newSessions[scope] = null
      this.audioDictations.newSession = this.audioDictations.newSessions[scope]
      if (ownerUserId) {
        this.persistDrafts()
        const request = this.loadQueuedMessages(ownerUserId, loadVersion)
        this.outboxLoadPromise = markRaw(request)
        void request.finally(() => {
          if (this.outboxLoadPromise === request) this.outboxLoadPromise = null
        })
      }
    },
    async loadQueuedMessages(ownerUserId = this.draftOwnerUserId, loadVersion = this.outboxLoadVersion) {
      if (ownerUserId === this.draftOwnerUserId && loadVersion === this.outboxLoadVersion) {
        this.outboxLoaded = false
        this.outboxLoadFailed = false
      }
      try {
        const entries = await loadChatOutbox(ownerUserId)
        if (ownerUserId !== this.draftOwnerUserId || loadVersion !== this.outboxLoadVersion) return
        this.queuedMessages = groupedOutbox(entries.map((entry) => ({
          ...entry,
          attachments: (entry.attachments || []).map((attachment) => ({
            ...attachment,
            file: markRaw(attachment.file)
          }))
        })))
        this.queuedSendPausedSessions = Object.fromEntries(
          Object.entries(this.queuedMessages)
            .filter(([, queue]) => queue[0]?.paused)
            .map(([key]) => [key, true])
        )
        this.outboxLoaded = true
        this.scheduleQueueDrain(0)
      } catch (error) {
        if (ownerUserId === this.draftOwnerUserId && loadVersion === this.outboxLoadVersion) {
          this.outboxLoadFailed = true
        }
      }
    },
    async fetchDefaults(projectId = this.sessionsProjectId, templateId = null) {
      const scope = sessionScopeKey(projectId)
      const targetDraft = this.drafts.newSessions[scope] || this.drafts.newSession
      const options = targetDraft.options
      const initial = !options.sessionTemplateId && !options.modelId && !options.sessionProfile && !options.agentProfileId && !options.trustLevel && options.memoryMode === 'full' && !options.reasoningEffort
      const snapshot = JSON.stringify(options)
      this.sessionSettingsPending += 1
      const request = (async () => {
        const { data } = await api.post('/sessions/defaults', {
          project_id: projectId,
          session_template_id: templateId || options.sessionTemplateId,
          ...(!initial && !templateId ? {
            agent_profile_id: options.agentProfileId, model_id: options.modelId,
            reasoning_effort: options.reasoningEffort, memory_mode: options.memoryMode,
            session_profile: options.sessionProfile
          } : {})
        })
        if (this.draftPreviewRequests[scope] !== request || (this.drafts.newSessions[scope] || this.drafts.newSession) !== targetDraft || JSON.stringify(options) !== snapshot) return
        if (initial && !templateId) {
          const defaults = sanitizedSessionOptions({ ...defaultSessionOptions(), ...data })
          if (sessionScopeKey(this.sessionsProjectId) === scope) this.sessionDefaults = defaults
          Object.assign(options, defaults)
        } else {
          for (const field of ['sessionTemplateId', 'sessionProfile', 'agentProfileId', 'modelId', 'reasoningEffort', 'memoryMode', 'effectiveMemoryMode', 'memoryToolsAvailable', 'memoryUnavailableReason']) {
            if (Object.hasOwn(data, field)) options[field] = data[field]
          }
          if (templateId && Object.hasOwn(data, 'workspaceMode')) {
            options.workspaceMode = options.preferredExecutionAgentId === data.preferredExecutionAgentId
              && options.currentDirectory === data.currentDirectory ? data.workspaceMode : 'local'
          }
          Object.assign(options, sanitizedSessionOptions(options))
        }
        this.persistDrafts()
      })()
      this.draftPreviewRequests[scope] = markRaw(request)
      try {
        await request
      } finally {
        if (this.draftPreviewRequests[scope] === request) delete this.draftPreviewRequests[scope]
        this.sessionSettingsPending = Math.max(0, this.sessionSettingsPending - 1)
      }
    },
    async waitForDraftDefaults(projectId = this.sessionsProjectId) {
      const scope = sessionScopeKey(projectId)
      while (this.draftPreviewRequests[scope]) await this.draftPreviewRequests[scope]
    },
    async reconcileNewSession(validModelIds, searchAvailable) {
      const options = this.drafts.newSession.options
      if (options.modelId && !validModelIds.includes(options.modelId)) {
        options.modelId = validModelIds.includes(this.sessionDefaults.modelId)
          ? this.sessionDefaults.modelId
          : null
        await this.fetchDefaults()
      }
      if (!searchAvailable && options.webSearchPolicy === 'required') {
        options.webSearchPolicy = 'available'
      }
      this.persistDrafts()
    },
    setDraft(message) {
      if (this.current) this.drafts.sessionMessages[this.current.id] = message
      else this.drafts.newSession.message = message
      this.persistDrafts()
    },
    hasNewSessionDraft(projectId = this.sessionsProjectId) {
      const scope = sessionScopeKey(projectId)
      return Boolean(this.drafts.newSessions[scope]?.message
        || this.attachmentDrafts.newSessions[scope]?.length
        || this.audioDictations.newSessions[scope]
        || this.queuedMessages[newSessionQueueKey(projectId)]?.length)
    },
    moveNewSessionDraft(sourceProjectId, targetProjectId) {
      const source = sessionScopeKey(sourceProjectId)
      const target = sessionScopeKey(targetProjectId)
      const draft = this.drafts.newSessions[source]
      if (this.current || source === target || !draft
        || sessionScopeKey(this.sessionsProjectId) !== target
        || this.hasNewSessionDraft(targetProjectId)) return false
      this.drafts.newSession.message = draft.message
      draft.message = ''
      this.attachmentDrafts.newSessions[target] = this.attachmentDrafts.newSessions[source] || []
      this.attachmentDrafts.newSession = this.attachmentDrafts.newSessions[target]
      this.attachmentDrafts.newSessions[source] = []
      this.audioDictations.newSessions[target] = this.audioDictations.newSessions[source] || null
      this.audioDictations.newSession = this.audioDictations.newSessions[target]
      this.audioDictations.newSessions[source] = null
      this.persistDrafts()
      return true
    },
    addAttachmentFiles(files, {
      sessionId = this.current?.id || null,
      projectId = this.sessionsProjectId,
      voiceMessage = false
    } = {}) {
      const limits = useInstanceLimitsStore()
      const incoming = Array.from(files || [])
      const scope = sessionScopeKey(projectId)
      const existing = sessionId
        ? this.attachmentDrafts.sessionMessages[sessionId] || []
        : this.attachmentDrafts.newSessions[scope] || []
      if (existing.length + incoming.length > limits.maxAttachmentsPerMessage) {
        return { added: 0, error: `Du kannst höchstens ${limits.maxAttachmentsPerMessage} Dateien pro Nachricht anhängen.` }
      }
      for (const file of incoming) {
        if (!Number.isFinite(file?.size) || file.size <= 0) {
          return { added: 0, error: 'Die ausgewählte Datei ist leer.' }
        }
        if (file.size > limits.maxUploadBytes) {
          return { added: 0, error: `Eine Datei darf höchstens ${limits.maxUploadMib} MiB groß sein.` }
        }
      }
      const incomingBytes = incoming.reduce((total, file) => total + file.size, 0)
      const messageBytes = existing.reduce(
        (total, attachment) => total + (Number(attachment.file?.size) || 0),
        0
      )
      if (messageBytes + incomingBytes > limits.maxMessageUploadBytes) {
        return {
          added: 0,
          error: `Die Anhänge einer Nachricht dürfen zusammen höchstens ${limits.maxMessageUploadMib} MiB groß sein.`
        }
      }
      const draftBytes = [
        ...Object.values(this.attachmentDrafts.newSessions).flat(),
        ...Object.values(this.attachmentDrafts.sessionMessages).flat(),
        ...Object.values(this.queuedMessages).flat().flatMap((entry) => entry.attachments)
      ].reduce((total, attachment) => total + (Number(attachment.file?.size) || 0), 0)
      if (draftBytes + incomingBytes > MAX_ATTACHMENT_DRAFT_BYTES) {
        return {
          added: 0,
          error: 'Dateianhänge dürfen über alle Sessions zusammen höchstens 1024 MiB belegen.'
        }
      }
      if (sessionId) this.attachmentDrafts.sessionMessages[sessionId] = existing
      else {
        this.attachmentDrafts.newSessions[scope] = existing
        if (scope === sessionScopeKey(this.sessionsProjectId)) this.attachmentDrafts.newSession = existing
      }
      const target = existing
      for (const file of incoming) {
        target.push({
          id: globalThis.crypto?.randomUUID?.() || `attachment-${Date.now()}-${target.length}`,
          file: markRaw(file),
          kind: attachmentKind(file),
          mimeType: normalizedMimeType(file),
          voiceMessage: voiceMessage === true,
          objectUrl: attachmentPreviewUrl(file)
        })
      }
      return { added: incoming.length, error: null }
    },
    removeAttachmentFile(id) {
      const target = this.activeAttachmentDraft
      const index = target.findIndex((attachment) => attachment.id === id)
      if (index < 0) return
      const [attachment] = target.splice(index, 1)
      if (attachment.objectUrl) globalThis.URL?.revokeObjectURL?.(attachment.objectUrl)
    },
    clearAttachmentDraft(sessionId = this.current?.id || null) {
      const target = sessionId
        ? this.attachmentDrafts.sessionMessages[sessionId] || []
        : this.attachmentDrafts.newSession
      for (const attachment of target) {
        if (attachment.objectUrl) globalThis.URL?.revokeObjectURL?.(attachment.objectUrl)
      }
      if (sessionId) delete this.attachmentDrafts.sessionMessages[sessionId]
      else {
        const target = []
        this.attachmentDrafts.newSession = target
        this.attachmentDrafts.newSessions[sessionScopeKey(this.sessionsProjectId)] = target
      }
    },
    clearAllAttachmentDrafts() {
      const newSessionAttachments = [...new Set([
        ...Object.values(this.attachmentDrafts.newSessions || {}),
        this.attachmentDrafts.newSession
      ])]
      for (const attachment of [
        ...newSessionAttachments.flat(),
        ...Object.values(this.attachmentDrafts.sessionMessages).flat()
      ]) {
        if (attachment.objectUrl) globalThis.URL?.revokeObjectURL?.(attachment.objectUrl)
      }
      this.attachmentDrafts = { newSession: [], sessionMessages: {} }
      this.attachmentDrafts.newSessions = { standalone: this.attachmentDrafts.newSession }
      this.audioDictations = { newSession: null, newSessions: { standalone: null }, sessionMessages: {} }
    },
    clearAllDrafts() {
      const ownerUserId = this.draftOwnerUserId
      this.cancelQueueDrain()
      this.clearAllAttachmentDrafts()
      releaseAttachmentDrafts(
        Object.values(this.queuedMessages).flat().flatMap((entry) => entry.attachments)
      )
      this.queuedMessages = {}
      this.queuedSendPausedSessions = {}
      this.outboxLoaded = !ownerUserId
      this.outboxLoadVersion += 1
      this.drafts = emptyDrafts()
      const scope = sessionScopeKey(this.sessionsProjectId)
      if (scope !== 'standalone') {
        this.drafts.newSessions[scope] = normalizedNewSessionDraft(null)
        this.drafts.newSession = this.drafts.newSessions[scope]
        this.attachmentDrafts.newSessions[scope] = []
        this.attachmentDrafts.newSession = this.attachmentDrafts.newSessions[scope]
        this.audioDictations.newSessions[scope] = null
        this.audioDictations.newSession = null
      }
      if (typeof window !== 'undefined') clearPersistedDrafts()
      return ownerUserId ? clearChatOutbox(ownerUserId) : Promise.resolve()
    },
    attachmentReplacementError(files, sessionId, sourceSessionId = sessionId) {
      const limits = useInstanceLimitsStore()
      if (files.length > limits.maxAttachmentsPerMessage) {
        return `Du kannst höchstens ${limits.maxAttachmentsPerMessage} Dateien pro Nachricht anhängen.`
      }
      for (const file of files) {
        if (!Number.isFinite(file?.size) || file.size <= 0) {
          return 'Die wiederhergestellte Datei ist leer.'
        }
        if (file.size > limits.maxUploadBytes) {
          return `Eine Datei darf höchstens ${limits.maxUploadMib} MiB groß sein.`
        }
      }
      const incomingBytes = files.reduce((total, file) => total + file.size, 0)
      if (incomingBytes > limits.maxMessageUploadBytes) {
        return `Die Anhänge einer Nachricht dürfen zusammen höchstens ${limits.maxMessageUploadMib} MiB groß sein.`
      }
      const replaced = [
        ...(sessionId
          ? this.attachmentDrafts.sessionMessages[sessionId] || []
          : this.attachmentDrafts.newSession),
        ...(sourceSessionId && sourceSessionId !== sessionId
          ? this.attachmentDrafts.sessionMessages[sourceSessionId] || []
          : [])
      ]
      const currentBytes = [
        ...Object.values(this.attachmentDrafts.newSessions).flat(),
        ...Object.values(this.attachmentDrafts.sessionMessages).flat(),
        ...Object.values(this.queuedMessages).flat().flatMap((entry) => entry.attachments)
      ].reduce((total, attachment) => total + (Number(attachment.file?.size) || 0), 0)
      const replacedBytes = replaced.reduce(
        (total, attachment) => total + (Number(attachment.file?.size) || 0),
        0
      )
      return currentBytes - replacedBytes + incomingBytes > MAX_ATTACHMENT_DRAFT_BYTES
        ? 'Dateianhänge dürfen über alle Sessions zusammen höchstens 256 MiB belegen.'
        : null
    },
    replaceAttachmentFiles(files, sessionId, attachments = []) {
      this.clearAttachmentDraft(sessionId)
      const target = files.map((file, index) => ({
        id: globalThis.crypto?.randomUUID?.() || `attachment-${Date.now()}-${index}`,
        file: markRaw(file),
        kind: attachmentKind(file),
        mimeType: normalizedMimeType(file),
        voiceMessage: (attachments[index]?.voiceMessage === true || attachments[index]?.metadata?.voiceMessage === true)
          && attachments[index]?.metadata?.voiceTranscriptionStatus !== 'completed',
        objectUrl: attachmentPreviewUrl(file)
      }))
      if (sessionId) this.attachmentDrafts.sessionMessages[sessionId] = target
      else {
        this.attachmentDrafts.newSession = target
        this.attachmentDrafts.newSessions[sessionScopeKey(this.sessionsProjectId)] = target
      }
    },
    getAudioDictation(sessionId = null, projectId = this.sessionsProjectId) {
      return sessionId
        ? this.audioDictations.sessionMessages[sessionId] || null
        : this.audioDictations.newSessions[sessionScopeKey(projectId)] || null
    },
    setAudioDictation(entry, sessionId = null, projectId = this.sessionsProjectId) {
      const value = entry ? {
        ...entry,
        ...(entry.file ? { file: markRaw(entry.file) } : {}),
        ...(entry.files ? { files: entry.files.map((file) => markRaw(file)) } : {})
      } : null
      if (sessionId) {
        if (value) this.audioDictations.sessionMessages[sessionId] = value
        else delete this.audioDictations.sessionMessages[sessionId]
      } else {
        const scope = sessionScopeKey(projectId)
        if (scope === sessionScopeKey(this.sessionsProjectId)) this.audioDictations.newSession = value
        this.audioDictations.newSessions[scope] = value
      }
    },
    resetNewDraft(projectId = this.sessionsProjectId) {
      const scope = sessionScopeKey(projectId)
      for (const attachment of this.attachmentDrafts.newSessions[scope] || []) {
        if (attachment.objectUrl) globalThis.URL?.revokeObjectURL?.(attachment.objectUrl)
      }
      const draft = {
        message: '',
        options: defaultSessionOptions()
      }
      this.drafts.newSessions[scope] = draft
      this.attachmentDrafts.newSessions[scope] = []
      this.audioDictations.newSessions[scope] = null
      if (scope === sessionScopeKey(this.sessionsProjectId)) {
        this.drafts.newSession = draft
        this.attachmentDrafts.newSession = this.attachmentDrafts.newSessions[scope]
        this.audioDictations.newSession = null
      }
      this.persistDrafts()
      void this.fetchDefaults(projectId).catch(() => {})
    },
}
