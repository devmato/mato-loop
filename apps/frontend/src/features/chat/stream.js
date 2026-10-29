import { markRaw } from 'vue'
import { api } from 'src/boot/axios'
import { useAgentIdentityStore } from 'src/stores/agent-identity'
import { useApprovalStore } from 'src/stores/approvals'
import { useControlCenterStore } from 'src/stores/control-center'
import { useQuestionStore } from 'src/stores/questions'
import { useSubagentStore } from 'src/stores/subagents'
import { useWorkspaceStore } from 'src/stores/workspace'
import { mergeResearchSources } from 'src/utils/research-sources'
import { managedSessionFileTarget, toolFileActions, toolOutputAttachments } from 'src/utils/attachments'
import { notifyError } from 'src/utils/notifications'
import { ACTIVE_TITLE_GENERATION_STATUSES, FILESYSTEM_MUTATION_TOOLS, controlCenterFileTarget, mergeMessages } from 'src/features/chat/model'

export const streamActions = {
    watchRun(sessionId, runId, run = {}) {
      if (this.current?.id === sessionId && this.current.latestRunId !== runId) {
        if (this.current.todos?.every((todo) => ['completed', 'cancelled'].includes(todo.status))) {
          this.current.todos = []
        }
        this.current.latestRunId = runId
      }
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
      if (!run.reconnecting) this.reconnectAttempts = 0
      this.eventSource?.close()
      const source = new EventSource(`/api/sessions/${sessionId}/runs/${runId}/events`)
      const workspace = useWorkspaceStore()
      const controlCenter = useControlCenterStore()
      const refreshChangedFiles = async (session) => {
        if (!session || session.activeRun || this.current?.id !== sessionId || this.selectedSessionId !== sessionId) return
        const actions = new Map()
        for (const message of session.messages || []) {
          if (message.role !== 'tool' || message.runId !== runId) continue
          const tool = session.messages.flatMap((item) => item.toolCalls || []).find((item) => item.id === message.toolCallId)
          for (const action of [
            ...toolFileActions(message).filter((action) => action.operation !== 'provide'),
            ...toolOutputAttachments(tool, message.attachments).map((attachment) => managedSessionFileTarget(attachment, sessionId)).filter(Boolean)
          ]) {
            const target = workspace.fileTargetFromAction(action)
            if (target) {
              const key = JSON.stringify([target.scope, target.rootIdentity || target.executionAgentId, target.path])
              actions.delete(key)
              actions.set(key, target)
            }
          }
        }
        for (const target of actions.values()) {
          if (this.current?.id !== sessionId || this.selectedSessionId !== sessionId || this.running) return
          const sameOpenFile = workspace.viewedEntry?.path === target.path
            && workspace.viewedEntry?.revision == null
            && workspace.scope === target.scope
            && (target.scope === 'session' ? workspace.rootIdentity === target.rootIdentity
              : workspace.workspaceContext?.executionAgentId === target.executionAgentId)
          workspace.noteFileTabMutations(controlCenter.fileTabsForTarget(target)
            .filter((tab) => tab.fileTarget.revision == null && (!sameOpenFile || tab.tabId !== workspace.activeFileTabId)).map((tab) => tab.tabId))
          if (sameOpenFile) workspace.noteFileMutation(target.path)
        }
        if (actions.size) void workspace.refreshStorageUsage(sessionId)
      }
      let finished = false
      this.eventSource = markRaw(source)
      this.runSessionId = sessionId
      this.runId = runId
      this.runAgentProfileId = run.agentProfileId || null
      this.running = !this.cancelRequested
      if (!run.reconnecting) this.runWarnings = []
      if (run.status) this.setSessionRunStatus(sessionId, run.status)
      this.streamModelName = run.modelName || this.streamModelName
      source.onopen = () => {
        if (this.eventSource !== source) return
        if (useSubagentStore().parentId === sessionId) void useSubagentStore().loadChildren()
        this.reconnectAttempts = 0
        if (this.activity.phase === 'reconnecting') {
          const connectedPhase = run.status === 'waiting_approval' || run.status === 'waiting_input'
            ? run.status
            : run.status === 'queued' ? 'queued' : 'preparing_context'
          this.setActivity(connectedPhase, { ...run, startedAt: run.createdAt })
        }
      }
      if (run.reconnecting) {
        this.setActivity('reconnecting', { ...run, startedAt: run.createdAt })
      } else {
        const resumedPhase = run.status === 'waiting_approval' || run.status === 'waiting_input'
          ? run.status
          : run.status === 'queued' ? 'queued' : 'preparing_context'
        this.setActivity(resumedPhase, { ...run, startedAt: run.createdAt })
      }
      source.addEventListener('run.started', (event) => {
        if (this.eventSource !== source) return
        const payload = JSON.parse(event.data)
        this.runAgentProfileId = payload.agentProfileId || this.runAgentProfileId
        this.streamModelName = payload.modelName || this.streamModelName
        this.setSessionRunStatus(sessionId, 'running')
        this.setActivity('preparing_context', { ...payload, startedAt: payload.createdAt })
      })
      source.addEventListener('run.snapshot', (event) => {
        if (this.eventSource !== source) return
        const payload = JSON.parse(event.data)
        this.streamContent = ''
        this.streamReasoning = null
        this.streamContextSummary = null
        this.streamResearchSources = mergeResearchSources([], payload.researchSources)
        this.runWarnings = []
        this.processOutputs = {}
        this.completedToolCallId = null
        // Rebuild the presentation without discarding the current run clock.
        if (payload.events?.length) this.setActivity('idle')
        for (const item of payload.events || []) {
          source.dispatchEvent(new MessageEvent(item.type, { data: JSON.stringify(item) }))
        }
        if (payload.content) source.dispatchEvent(new MessageEvent('message.delta', {
          data: JSON.stringify({ delta: payload.content })
        }))
        void this.refreshSession(sessionId).catch(() => {})
        void useApprovalStore().fetchPending().catch(() => {})
        void useQuestionStore().fetchPending().catch(() => {})
        if (useSubagentStore().parentId === sessionId) void useSubagentStore().loadChildren()
      })
      source.addEventListener('run.timing', (event) => {
        if (this.eventSource !== source) return
        this.setRunTiming(JSON.parse(event.data))
      })
      source.addEventListener('run.phase', (event) => {
        if (this.eventSource !== source) return
        const payload = JSON.parse(event.data)
        this.setActivity(payload.phase, payload)
        if (payload.phase !== 'compacting_context') this.streamContextSummary = null
      })
      source.addEventListener('session.title.updated', (event) => {
        if (this.eventSource !== source) return
        const payload = JSON.parse(event.data)
        if (this.current?.id === payload.sessionId) {
          this.current.title = payload.title
          this.current.titleGenerationStatus = 'completed'
        }
        this.updateCachedSession(payload.sessionId, (session) => ({
          ...session,
          title: payload.title,
          titleGenerationStatus: 'completed'
        }))
      })
      source.addEventListener('agent.identity.updated', (event) => {
        if (this.eventSource !== source) return
        useAgentIdentityStore().assignIdentity(JSON.parse(event.data).identity)
      })
      source.addEventListener('subagent.updated', () => {
        if (this.eventSource !== source || useSubagentStore().parentId !== sessionId) return
        void useSubagentStore().loadChildren()
      })
      source.addEventListener('message.delta', (event) => {
        if (this.eventSource !== source) return
        if (this.activity.phase !== 'streaming') this.setActivity('streaming')
        this.streamContent += JSON.parse(event.data).delta
      })
      source.addEventListener('reasoning.updated', (event) => {
        if (this.eventSource !== source) return
        const payload = JSON.parse(event.data)
        this.streamReasoning = payload.reasoning
        if (typeof payload.activityLabel === 'string' && payload.activityLabel.trim()) {
          const reasoningLabel = payload.activityLabel.trim()
          if (reasoningLabel !== 'Nachdenken' || !this.activity.reasoningLabel) {
            this.activity = { ...this.activity, reasoningLabel }
          }
        }
      })
      source.addEventListener('context.summary.updated', (event) => {
        if (this.eventSource !== source) return
        if (this.activity.phase !== 'compacting_context') this.setActivity('compacting_context')
        const summary = JSON.parse(event.data).summary
        this.streamContextSummary = summary && typeof summary.content === 'string'
          ? summary
          : null
      })
      source.addEventListener('run.warning', (event) => {
        if (this.eventSource !== source) return
        const warning = JSON.parse(event.data)
        if (typeof warning.code !== 'string' || this.runWarnings.some((item) => (
          item.code === warning.code && item.modelRequest === warning.modelRequest
        ))) return
        this.runWarnings.push({ code: warning.code, modelRequest: warning.modelRequest })
      })
      source.addEventListener('context.compacted', (event) => {
        if (this.eventSource !== source) return
        const summary = JSON.parse(event.data).summary
        if (summary?.id && this.current?.id === sessionId) {
          this.current.contextSummaries = mergeMessages(
            this.current.contextSummaries,
            [summary]
          )
        }
        this.streamContextSummary = null
        if (this.activity.phase === 'compacting_context') this.setActivity('preparing_context')
        void this.refreshSession(sessionId).catch(() => {})
      })
      source.addEventListener('process.output', (event) => {
        if (this.eventSource !== source) return
        const payload = JSON.parse(event.data)
        const toolCallId = payload.toolCallId
        if (!toolCallId) return
        const incoming = payload.output && typeof payload.output === 'object'
          ? payload.output
          : payload
        const previous = this.processOutputs[toolCallId] || { stdout: '', stderr: '' }
        const next = { ...previous }
        if (typeof incoming.stdout === 'string') next.stdout = incoming.stdout
        if (typeof incoming.stderr === 'string') next.stderr = incoming.stderr
        const stream = incoming.stream || incoming.event
        const content = incoming.content ?? incoming.data
        if (['stdout', 'stderr'].includes(stream)) {
          if (typeof incoming.delta === 'string') next[stream] = (next[stream] || '') + incoming.delta
          else if (typeof content === 'string') next[stream] = content
        }
        if (typeof incoming.truncated === 'boolean') next.truncated = incoming.truncated
        if (typeof incoming.outputTruncated === 'boolean') next.truncated = incoming.outputTruncated
        this.processOutputs = { ...this.processOutputs, [toolCallId]: next }
      })
      const promoteStreamedMessage = (message) => {
        if (message && this.current?.id === sessionId) {
          if (!this.current.messages) this.current.messages = []
          const messages = this.current.messages
          this.streamModelName = message.modelName || this.streamModelName
          const promoted = {
            ...message,
            content: message.content,
            reasoning: message.reasoning,
            modelName: this.streamModelName
          }
          const index = messages.findIndex((item) => item.id === message.id)
          if (index === -1) messages.push(promoted)
          else messages[index] = { ...messages[index], ...promoted }
        }
        this.streamContent = ''
        this.streamReasoning = null
        this.streamContextSummary = null
      }
      source.addEventListener('message.updated', () => {
        if (this.eventSource !== source || this.current?.id !== sessionId) return
        void this.refreshSession(sessionId).catch(() => {})
      })
      source.addEventListener('message.completed', (event) => {
        if (this.eventSource !== source) return
        const payload = JSON.parse(event.data)
        promoteStreamedMessage(payload.message)
        this.runWarnings = []
        const tool = payload.message?.toolCalls?.[0]
        this.setActivity(tool ? 'processing_tool' : 'finalizing', tool ? {
          tool: tool.name,
          toolCallId: tool.id,
          toolDisplay: tool.toolDisplay
        } : { activityLabel: payload.activityLabel })
        this.refreshSession(sessionId)
      })
      source.addEventListener('message.interrupted', (event) => {
        if (this.eventSource !== source) return
        promoteStreamedMessage(JSON.parse(event.data).message)
        this.runWarnings = []
      })
      source.addEventListener('message.continued', () => {
        if (this.eventSource !== source) return
        this.streamContent = ''
        this.streamReasoning = null
        this.streamContextSummary = null
        this.runWarnings = []
      })
      source.addEventListener('tool.completed', (event) => {
        if (this.eventSource !== source) return
        const payload = JSON.parse(event.data)
        this.streamResearchSources = mergeResearchSources(
          this.streamResearchSources,
          payload.metadata?.sources
        )
        this.completedToolCallId = payload.toolCallId || null
        const refresh = this.refreshSession(sessionId)
        void refresh?.then(async (session) => {
          if (!session || this.current?.id !== sessionId) return
          const result = session.messages?.find((message) => (
            message.role === 'tool' && message.toolCallId === payload.toolCallId
          ))
          const workspace = useWorkspaceStore()
          const attachmentAction = result?.attachments
            ?.map((attachment) => managedSessionFileTarget(attachment, sessionId))
            .find(Boolean)
          const toolCall = session.messages
            ?.flatMap((message) => message.toolCalls || [])
            .find((item) => item.id === payload.toolCallId)
          const argumentsValue = toolCall?.arguments || {}
          const sourceRoot = argumentsValue.source_root || argumentsValue.root
          const managedRootMutation = FILESYSTEM_MUTATION_TOOLS.has(payload.tool) && [
            argumentsValue.root,
            argumentsValue.source_root,
            argumentsValue.destination_root
          ].some((root) => ['files', 'session'].includes(root))
          if (
            !payload.metadata?.errorCode
            && (managedRootMutation || attachmentAction?.root === 'session')
          ) void workspace.refreshStorageUsage(sessionId)
          const sourcePath = argumentsValue.source_path || argumentsValue.path
          if (
            !payload.metadata?.errorCode
            && sourcePath
            && ['delete_path', 'move_path'].includes(payload.tool)
          ) {
            const controlCenter = useControlCenterStore()
            const sourceTarget = controlCenterFileTarget(sourceRoot, sourcePath, session)
            if (sourceTarget && payload.tool === 'move_path') {
              const destinationTarget = controlCenterFileTarget(
                argumentsValue.destination_root,
                argumentsValue.destination_path,
                session
              )
              if (destinationTarget) {
                const reboundTabs = await controlCenter.rebindFileTargets(sourceTarget, destinationTarget)
                workspace.rebindFileTabPaths(reboundTabs, sourceTarget.path, destinationTarget.path)
              }
            } else if (sourceTarget) {
              const affectedTabs = controlCenter.fileTabsForTarget(sourceTarget)
              const dirtyTabIds = affectedTabs
                .filter((tab) => workspace.fileTabDirty(tab.tabId))
                .map((tab) => tab.tabId)
              workspace.noteFileTabMutations(dirtyTabIds)
              const cleanTabIds = affectedTabs
                .filter((tab) => !dirtyTabIds.includes(tab.tabId))
                .map((tab) => tab.tabId)
              const removedTabs = await controlCenter.rebindFileTargets(sourceTarget, null, cleanTabIds)
              removedTabs.forEach((tabId) => workspace.discardFileTab(tabId))
            }
          }
          const sourceMatchesOpenFile = ['delete_path', 'move_path'].includes(payload.tool)
            && workspace.viewedEntry?.path
            && (workspace.viewedEntry.path === sourcePath
              || workspace.viewedEntry.path.startsWith(`${sourcePath}/`))
            && (!['files', 'session'].includes(sourceRoot) || workspace.rootIdentity === sessionId)
            && ((sourceRoot === 'files' && workspace.scope === 'session')
              || (sourceRoot === 'computer' && workspace.scope === 'filesystem'
                && workspace.workspaceContext?.executionAgentId === session.preferredExecutionAgentId))
          if (sourceMatchesOpenFile) workspace.noteFileMutation(workspace.viewedEntry.path)
        }).catch(() => {})
        if (FILESYSTEM_MUTATION_TOOLS.has(payload.tool) && !payload.metadata?.errorCode) {
          const workspace = useWorkspaceStore()
          if (
            workspace.sessionId === sessionId
            && (workspace.scope !== 'session' || workspace.rootIdentity === sessionId)
          ) void workspace.refresh()
        }
      })
      source.addEventListener('todo.updated', (event) => {
        if (this.eventSource !== source || this.current?.id !== sessionId) return
        this.current.todos = JSON.parse(event.data).todos
      })
      source.addEventListener('approval.created', (event) => {
        if (this.eventSource !== source) return
        const payload = JSON.parse(event.data)
        const approval = payload.approval
        if (approval.payload?.subagentSessionId) {
          useApprovalStore().add(approval)
          return
        }
        this.setActivity('waiting_approval', {
          tool: approval.details?.tool,
          toolCallId: approval.details?.toolCallId,
          activityLabel: payload.activityLabel
        })
        this.setSessionRunStatus(sessionId, 'waiting_approval')
        useApprovalStore().add(approval)
      })
      source.addEventListener('approval.resolved', (event) => {
        if (this.eventSource !== source) return
        if (JSON.parse(event.data).subagentSessionId) {
          void useApprovalStore().fetchPending().catch(() => {})
          return
        }
        this.setActivity('processing_tool', {
          tool: this.activity.tool,
          toolCallId: this.activity.toolCallId
        })
        this.setSessionRunStatus(sessionId, 'running')
        useApprovalStore().fetchPending()
      })
      source.addEventListener('approval.expired', (event) => {
        if (this.eventSource !== source) return
        if (JSON.parse(event.data).subagentSessionId) {
          void useApprovalStore().fetchPending().catch(() => {})
          return
        }
        this.setActivity('processing_tool', {
          tool: this.activity.tool,
          toolCallId: this.activity.toolCallId
        })
        this.setSessionRunStatus(sessionId, 'running')
        useApprovalStore().fetchPending()
      })
      source.addEventListener('question.created', (event) => {
        if (this.eventSource !== source) return
        const payload = JSON.parse(event.data)
        const question = payload.question
        if (question.payload?.subagentSessionId) {
          useQuestionStore().add(question)
          return
        }
        this.setActivity('waiting_input', {
          tool: 'question',
          toolCallId: question.toolCallId,
          activityLabel: payload.activityLabel
        })
        this.setSessionRunStatus(sessionId, 'waiting_input')
        useQuestionStore().add(question)
      })
      for (const name of ['question.answered', 'question.rejected', 'question.expired']) {
        source.addEventListener(name, (event) => {
          if (this.eventSource !== source) return
          if (JSON.parse(event.data).subagentSessionId) {
            void useQuestionStore().fetchPending().catch(() => {})
            return
          }
          this.setActivity('processing_tool', { tool: 'question', toolCallId: this.activity.toolCallId })
          this.setSessionRunStatus(sessionId, 'running')
          void useQuestionStore().fetchPending().catch(() => {})
        })
      }
      source.addEventListener('session.executionAgentChanged', () => {
        if (this.eventSource !== source) return
        void this.refreshSession(sessionId).catch(() => {})
      })
      const finish = () => {
        if (this.eventSource !== source) return
        const current = this.current?.id === sessionId ? this.current : null
        const titleGenerationActive = ACTIVE_TITLE_GENERATION_STATUSES.has(
          current?.titleGenerationStatus
        )
        finished = true
        clearTimeout(this.reconnectTimer)
        this.reconnectTimer = null
        this.reconnectAttempts = 0
        source.close()
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
        this.clearActivity()
        this.setSessionRunStatus(sessionId, null)
        if (useSubagentStore().parentId === sessionId) void useSubagentStore().loadChildren()
        if (!this.historyActionPending) {
          void this.refreshSession(sessionId)
            .then(refreshChangedFiles)
            .catch(() => {})
            .finally(() => {
              this.cancelQueueDrain()
              this.scheduleQueueDrain(0)
            })
          void this.refreshSessions().catch(() => {})
        } else {
          this.scheduleQueueDrain(0)
        }
        useApprovalStore().fetchPending()
        void useQuestionStore().fetchPending().catch(() => {})
        void useAgentIdentityStore().fetchIdentity({ suppressErrorToast: true }).catch(() => {})
        if (titleGenerationActive) void this.refreshGeneratedTitle(sessionId)
      }
      source.addEventListener('run.failed', () => {
        if (this.eventSource !== source) return
        finish()
      })
      for (const name of ['run.completed', 'run.cancelled']) {
        source.addEventListener(name, () => {
          finish()
        })
      }
      source.onerror = async () => {
        if (this.eventSource !== source) return
        source.close()
        this.eventSource = null
        this.streamContent = ''
        this.streamResearchSources = []
        this.streamReasoning = null
        this.streamContextSummary = null
        if (finished) return
        this.running = !this.cancelRequested
        if (!this.cancelRequested) this.setActivity('reconnecting')
        this.reconnectAttempts += 1
        if (useSubagentStore().parentId === sessionId) void useSubagentStore().loadChildren()
        void useApprovalStore().fetchPending().catch(() => {})
        void useQuestionStore().fetchPending().catch(() => {})

        const stopDisconnectedRun = (terminal = false) => {
          if (this.runId !== runId) return
          clearTimeout(this.reconnectTimer)
          this.reconnectTimer = null
          this.runSessionId = null
          this.runId = null
          this.runAgentProfileId = null
          this.running = false
          this.stopping = false
          this.cancelRequested = false
          this.streamModelName = null
          this.clearActivity()
          this.setSessionRunStatus(sessionId, null)
          void this.refreshSession(sessionId).then((session) => {
            if (terminal) return refreshChangedFiles(session)
          }).catch(() => {})
          this.refreshSessions()
          useApprovalStore().fetchPending()
          void useQuestionStore().fetchPending().catch(() => {})
        }
        const scheduleReconnect = (runState = {}) => {
          if (this.reconnectAttempts > 3) {
            notifyError({ code: 'stream.connectionLost' })
            stopDisconnectedRun()
            return
          }
          this.reconnectTimer = setTimeout(() => {
            if (this.selectedSessionId !== sessionId || this.runId !== runId) return
            this.watchRun(sessionId, runId, { ...runState, reconnecting: true })
          }, this.reconnectAttempts * 1000)
        }

        try {
          const { data } = await api.get(`/sessions/${sessionId}/runs/${runId}`, {
            suppressErrorToast: true
          })
          if (this.selectedSessionId !== sessionId || this.runId !== runId) return
          if (['queued', 'running', 'waiting_approval', 'waiting_input'].includes(data.status)) {
            scheduleReconnect(data)
          } else {
            stopDisconnectedRun(true)
          }
        } catch {
          scheduleReconnect({ createdAt: this.activity.startedAt })
        }
      }
    }
}
