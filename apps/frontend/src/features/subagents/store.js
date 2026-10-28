import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { api } from 'src/boot/axios'
import { emptyActivity } from 'src/features/chat/state'
import { mergeAgentActivity } from 'src/utils/agent-activity'
import { notifyError } from 'src/utils/notifications'
import { useApprovalStore } from 'src/stores/approvals'
import { useQuestionStore } from 'src/stores/questions'
import { mergeResearchSources } from 'src/utils/research-sources'

const ACTIVE = new Set(['queued', 'running', 'waiting_approval', 'waiting_input'])

export function subagentStatus(run) {
  return ({
    queued: 'Wartet', running: 'Arbeitet', waiting_approval: 'Freigabe erforderlich',
    waiting_input: 'Antwort erforderlich', completed: 'Abgeschlossen',
    failed: 'Fehlgeschlagen', cancelled: 'Abgebrochen'
  })[run?.status] || 'Noch nicht gestartet'
}

function mergeById(existing = [], incoming = []) {
  return [...new Map([...existing, ...incoming].map((item) => [item.id, item])).values()]
}

export const useSubagentStore = defineStore('subagents', () => {
  const parentId = ref(null)
  const children = ref([])
  const childrenLoading = ref(false)
  const childrenError = ref(null)
  const current = ref(null)
  const run = ref(null)
  const loading = ref(false)
  const error = ref(null)
  const olderLoading = ref(false)
  const streamContent = ref('')
  const streamResearchSources = ref([])
  const streamReasoning = ref(null)
  const streamContextSummary = ref(null)
  const processOutputs = ref({})
  const completedToolCallId = ref(null)
  const warnings = ref([])
  const activity = ref(emptyActivity())
  const reconnecting = ref(false)
  const running = computed(() => ACTIVE.has(run.value?.status))
  let listVersion = 0
  let listRequest = null
  let listDirty = false
  let selectedId = null
  let selectionVersion = 0
  let controller = null
  let source = null
  let reconnectTimer = null
  let reconnectAttempts = 0
  let refreshRequest = null
  let refreshDirty = false
  let messageRevision = 0
  let resetHistory = false

  async function loadChildren(id = parentId.value) {
    if (id !== parentId.value) {
      listVersion += 1
      parentId.value = id
      children.value = []
      childrenError.value = null
      listRequest = null
      close()
    }
    if (!id) {
      childrenLoading.value = false
      return
    }
    if (listRequest) {
      listDirty = true
      return listRequest
    }
    const version = listVersion
    childrenLoading.value = true
    const request = (async () => {
      do {
        listDirty = false
        try {
          const { data } = await api.get(`/sessions/${id}/children`)
          if (version !== listVersion) return
          if (listDirty) continue
          children.value = Array.isArray(data) ? data : []
          childrenError.value = null
        } catch (failure) {
          if (version === listVersion) childrenError.value = failure
          return
        }
      } while (listDirty && version === listVersion)
    })()
    listRequest = request
    try { await request } finally {
      if (listRequest === request) {
        listRequest = null
        childrenLoading.value = false
      }
    }
  }

  function close() {
    selectionVersion += 1
    selectedId = null
    source?.close()
    source = null
    controller?.abort()
    controller = null
    clearTimeout(reconnectTimer)
    reconnectTimer = null
    reconnectAttempts = 0
    refreshRequest = null
    refreshDirty = false
    resetHistory = false
    current.value = null
    run.value = null
    loading.value = false
    olderLoading.value = false
    error.value = null
    reconnecting.value = false
    clearStream()
    processOutputs.value = {}
    completedToolCallId.value = null
    activity.value = emptyActivity()
  }

  function clearStream({ preserveResearch = false } = {}) {
    streamContent.value = ''
    if (!preserveResearch) streamResearchSources.value = []
    streamReasoning.value = null
    streamContextSummary.value = null
    warnings.value = []
  }

  async function open(parent, id) {
    close()
    if (!parent || !id) return
    selectedId = id
    controller = new AbortController()
    loading.value = true
    const version = selectionVersion
    await refresh(parent)
    if (version !== selectionVersion) return
    loading.value = false
    if (current.value) connect(parent)
  }

  async function refresh(parent = parentId.value) {
    if (!selectedId) return
    if (refreshRequest) {
      refreshDirty = true
      return refreshRequest
    }
    const id = selectedId
    const version = selectionVersion
    const signal = controller.signal
    const request = (async () => {
      do {
        refreshDirty = false
        const revision = messageRevision
        try {
          const { data } = await api.get(`/sessions/${id}`, { signal })
          if (version !== selectionVersion) return
          if (data.id !== id || data.parentSessionId !== parent || data.readOnly !== true) {
            error.value = { code: 'session.notFound' }
            return
          }
          const knownRun = data.activeRun || run.value || children.value.find((child) => child.id === id)?.run
          const runData = knownRun?.id
            ? (await api.get(`/sessions/${id}/runs/${knownRun.id}`, { signal })).data
            : null
          if (version !== selectionVersion) return
          if (revision !== messageRevision) {
            refreshDirty = true
            continue
          }
          const previous = resetHistory ? null : current.value
          current.value = {
            ...data,
            messages: mergeById(previous?.messages, data.messages),
            contextSummaries: mergeById(previous?.contextSummaries, data.contextSummaries),
            nextMessageCursor: previous ? previous.nextMessageCursor : data.nextMessageCursor
          }
          run.value = runData
          resetHistory = false
          error.value = null
          if (!source) activity.value = mergeAgentActivity(emptyActivity(), {
            ...runData,
            phase: runData?.status || 'idle'
          })
        } catch (failure) {
          if (version === selectionVersion && !signal.aborted) error.value = failure
          return
        }
      } while (refreshDirty && version === selectionVersion)
    })()
    refreshRequest = request
    try { await request } finally {
      if (refreshRequest === request) refreshRequest = null
    }
  }

  async function loadOlder() {
    const cursor = current.value?.nextMessageCursor
    if (!cursor || olderLoading.value || reconnecting.value) return
    const version = selectionVersion
    olderLoading.value = true
    try {
      const { data } = await api.get(`/sessions/${selectedId}/messages`, {
        params: { cursor, pageSize: 20 }, signal: controller.signal
      })
      if (version !== selectionVersion || resetHistory || current.value?.nextMessageCursor !== cursor) return
      current.value.messages = mergeById(data.items, current.value.messages)
      current.value.contextSummaries = mergeById(data.contextSummaries, current.value.contextSummaries)
      current.value.nextMessageCursor = data.nextCursor || null
    } catch {
      // The Axios interceptor displays the error; the cursor remains available.
    } finally {
      if (version === selectionVersion) olderLoading.value = false
    }
  }

  function connect(parent) {
    if (!running.value || source || !selectedId) return
    const version = selectionVersion
    const connection = new EventSource(`/api/sessions/${selectedId}/runs/${run.value.id}/events`)
    source = connection
    // A broker snapshot replaces live state; durable messages are merged by ID.
    clearStream()
    const seenIds = new Set()
    const on = (name, handler) => connection.addEventListener(name, (event) => {
      if (source !== connection || version !== selectionVersion) return
      if (event.lastEventId && seenIds.has(event.lastEventId)) return
      if (event.lastEventId) seenIds.add(event.lastEventId)
      try { handler(JSON.parse(event.data)) } catch {
        connection.onerror()
      }
    })
    connection.onopen = () => {
      if (source === connection) reconnecting.value = false
    }
    on('run.started', (payload) => {
      messageRevision += 1
      run.value = { ...run.value, ...payload, status: 'running' }
      activity.value = mergeAgentActivity(activity.value, { ...payload, phase: 'preparing_context' })
    })
    on('run.snapshot', (payload) => {
      messageRevision += 1
      clearStream()
      streamResearchSources.value = mergeResearchSources([], payload.researchSources)
      processOutputs.value = {}
      completedToolCallId.value = null
      if (payload.events?.length) activity.value = emptyActivity(activity.value.revision + 1)
      for (const item of payload.events || []) {
        connection.dispatchEvent(new MessageEvent(item.type, { data: JSON.stringify(item) }))
      }
      if (payload.content) connection.dispatchEvent(new MessageEvent('message.delta', {
        data: JSON.stringify({ delta: payload.content })
      }))
      void refresh(parent)
      void useApprovalStore().fetchPending().catch(() => {})
      void useQuestionStore().fetchPending().catch(() => {})
    })
    on('run.phase', (payload) => {
      activity.value = mergeAgentActivity(activity.value, payload)
      if (payload.phase !== 'compacting_context') streamContextSummary.value = null
    })
    on('run.timing', (payload) => { activity.value = mergeAgentActivity(activity.value, { ...payload, phase: null }) })
    on('message.delta', (payload) => {
      if (typeof payload.delta === 'string') streamContent.value += payload.delta
      if (activity.value.phase !== 'streaming') activity.value = mergeAgentActivity(activity.value, { phase: 'streaming' })
    })
    on('reasoning.updated', (payload) => { streamReasoning.value = payload.reasoning })
    on('context.summary.updated', (payload) => {
      activity.value = mergeAgentActivity(activity.value, { phase: 'compacting_context' })
      streamContextSummary.value = payload.summary
    })
    on('message.continued', () => { clearStream({ preserveResearch: true }) })
    on('run.warning', (payload) => {
      if (!warnings.value.some((item) => item.code === payload.code && item.modelRequest === payload.modelRequest)) {
        warnings.value.push(payload)
      }
    })
    on('process.output', (payload) => {
      if (!payload.toolCallId) return
      const incoming = payload.output || payload
      const output = { ...processOutputs.value[payload.toolCallId] }
      for (const key of ['stdout', 'stderr']) {
        if (typeof incoming[key] === 'string') output[key] = incoming[key]
      }
      const stream = incoming.stream || incoming.event
      const content = incoming.content ?? incoming.data
      if (['stdout', 'stderr'].includes(stream)) {
        if (typeof incoming.delta === 'string') output[stream] = (output[stream] || '') + incoming.delta
        else if (typeof content === 'string') output[stream] = content
      }
      output.truncated = incoming.truncated ?? incoming.outputTruncated ?? output.truncated
      processOutputs.value[payload.toolCallId] = output
    })
    for (const name of ['message.completed', 'message.interrupted']) on(name, (payload) => {
      if (payload.message?.id && current.value) {
        current.value.messages = mergeById(current.value.messages, [payload.message])
        messageRevision += 1
      }
      clearStream({ preserveResearch: true })
      if (name === 'message.completed') {
        const tool = payload.message?.toolCalls?.[0]
        activity.value = mergeAgentActivity(activity.value, tool ? {
          phase: 'processing_tool',
          tool: tool.name,
          toolCallId: tool.id,
          toolDisplay: tool.toolDisplay
        } : { phase: 'finalizing', activityLabel: payload.activityLabel })
      }
      void refresh(parent)
    })
    for (const name of ['message.updated', 'tool.completed', 'context.compacted', 'session.title.updated']) {
      on(name, (payload) => {
        if (name === 'tool.completed') {
          streamResearchSources.value = mergeResearchSources(
            streamResearchSources.value,
            payload.metadata?.sources
          )
          completedToolCallId.value = payload.toolCallId || null
          delete processOutputs.value[payload.toolCallId]
        }
        if (name === 'context.compacted') {
          streamContextSummary.value = null
          if (activity.value.phase === 'compacting_context') {
            activity.value = mergeAgentActivity(activity.value, { phase: 'preparing_context' })
          }
        }
        messageRevision += 1
        void refresh(parent)
      })
    }
    for (const [name, status] of [['approval.created', 'waiting_approval'], ['question.created', 'waiting_input']]) {
      on(name, (payload) => {
        if (name === 'approval.created') useApprovalStore().add(payload.approval)
        messageRevision += 1
        run.value.status = status
        activity.value.phase = status
      })
    }
    for (const name of ['approval.resolved', 'approval.expired', 'question.answered', 'question.rejected', 'question.expired']) {
      on(name, () => {
        if (name.startsWith('approval.')) void useApprovalStore().fetchPending().catch(() => {})
        void refresh(parent)
      })
    }
    for (const status of ['completed', 'failed', 'cancelled']) on(`run.${status}`, () => {
      connection.close()
      source = null
      run.value.status = status
      clearStream()
      messageRevision += 1
      void refresh(parent)
      void loadChildren(parent)
    })
    connection.onerror = () => {
      if (source !== connection || version !== selectionVersion) return
      connection.close()
      source = null
      clearStream()
      processOutputs.value = {}
      completedToolCallId.value = null
      // A disconnect can span more than one page. Restart pagination from the
      // canonical head instead of retaining a cursor that skips the missing gap.
      resetHistory = true
      reconnecting.value = true
      const recover = async () => {
        if (version !== selectionVersion) return
        reconnectAttempts += 1
        await refresh(parent)
        if (version !== selectionVersion) return
        if (!error.value && !running.value) {
          reconnecting.value = false
          void loadChildren(parent)
        } else if (reconnectAttempts <= 3) {
          reconnectTimer = setTimeout(() => {
            if (version !== selectionVersion) return
            if (error.value) void recover()
            else connect(parent)
          }, reconnectAttempts * 1000)
        } else {
          reconnecting.value = false
          error.value = { code: 'stream.connectionLost' }
          notifyError(error.value)
        }
      }
      void recover()
    }
  }

  return {
    parentId, children, childrenLoading, childrenError, current, run, running,
    loading, error, olderLoading, streamContent, streamResearchSources, streamReasoning, streamContextSummary,
    processOutputs, completedToolCallId, warnings, activity, reconnecting, loadChildren, open, close, loadOlder
  }
})
