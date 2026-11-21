const DRAFT_STORAGE_KEY = 'matoLoopChatDrafts'

export function defaultSessionOptions() {
  return {
    agentProfileId: null,
    sessionTemplateId: null,
    sessionProfile: null,
    modelId: null,
    reasoningEffort: null,
    memoryMode: 'full',
    trustLevel: null,
    effectiveMemoryMode: 'full',
    memoryToolsAvailable: true,
    memoryUnavailableReason: null,
    webSearchPolicy: 'available',
    preferredExecutionAgentId: null,
    currentDirectory: null,
    workspaceMode: 'local',
    workspacePath: null,
    gitFetchBeforeActions: null,
    workspaceContext: null
  }
}

export function sanitizedSessionOptions(options) {
  const sanitized = { ...options, workspaceContext: null }
  if (!sanitized.preferredExecutionAgentId) sanitized.currentDirectory = null
  if (sanitized.sessionProfile?.worktreesEnabled === false) sanitized.workspaceMode = 'local'
  if (sanitized.workspaceMode !== 'worktree') sanitized.workspacePath = null
  return sanitized
}

export function sessionCreatePayload(options, projectId, controlCenter) {
  return {
    project_id: projectId,
    ...(options.sessionTemplateId ? { session_template_id: options.sessionTemplateId } : {}),
    ...(options.sessionProfile ? { session_profile: options.sessionProfile } : {}),
    ...(options.agentProfileId ? { agent_profile_id: options.agentProfileId } : {}),
    model_id: options.modelId,
    reasoning_effort: options.reasoningEffort,
    memory_mode: options.memoryMode,
    ...(options.trustLevel ? { trust_level: options.trustLevel } : {}),
    web_search_policy: options.webSearchPolicy,
    ...(typeof options.gitFetchBeforeActions === 'boolean' ? { git_fetch_before_actions: options.gitFetchBeforeActions } : {}),
    preferred_execution_agent_id: options.preferredExecutionAgentId,
    workspace_mode: projectId && options.preferredExecutionAgentId && options.currentDirectory ? options.workspaceMode || 'local' : 'local',
    ...(projectId && options.workspaceMode === 'worktree' && options.workspacePath ? { workspace_path: options.workspacePath } : {}),
    ...(options.preferredExecutionAgentId ? { current_directory: options.currentDirectory } : {}),
    control_center: controlCenter
  }
}

export function emptyDrafts() {
  const newSession = { message: '', options: defaultSessionOptions() }
  return {
    newSession,
    newSessions: { standalone: newSession },
    sessionMessages: {},
    sessionAgentProfiles: {}
  }
}

export function sessionScopeKey(projectId) {
  return projectId || 'standalone'
}

export function newSessionQueueKey(projectId) {
  return `new:${sessionScopeKey(projectId)}`
}

export function queueKeyForEntry(entry) {
  return entry.sessionId || newSessionQueueKey(entry.projectId)
}

export function groupedOutbox(entries) {
  const grouped = {}
  for (const entry of entries) {
    const key = queueKeyForEntry(entry)
    if (!grouped[key]) grouped[key] = []
    grouped[key].push(entry)
  }
  return grouped
}

export function emptySessionList() {
  return {
    items: [],
    loaded: false,
    loading: false,
    loadFailed: false,
    nextCursor: null,
    loadingMore: false,
    loadMoreFailed: false,
    loadRequest: null,
    pageRequest: null,
    refreshRequest: null,
    refreshQueued: null,
    stale: false,
    boundary: null,
    version: 0
  }
}

export function normalizedNewSessionDraft(value) {
  const options = value?.options || {}
  return {
    message: typeof value?.message === 'string' ? value.message : '',
    options: sanitizedSessionOptions({
      agentProfileId: typeof options.agentProfileId === 'string' ? options.agentProfileId : null,
      sessionTemplateId: typeof options.sessionTemplateId === 'string' ? options.sessionTemplateId : null,
      sessionProfile: options.sessionProfile || null,
      modelId: typeof options.modelId === 'string' ? options.modelId : null,
      reasoningEffort: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'].includes(options.reasoningEffort)
        ? options.reasoningEffort
        : null,
      memoryMode: ['full', 'read_only', 'disabled'].includes(options.memoryMode)
        ? options.memoryMode
        : 'full',
      trustLevel: ['private', 'public'].includes(options.trustLevel) ? options.trustLevel : null,
      effectiveMemoryMode: ['full', 'read_only', 'disabled'].includes(options.effectiveMemoryMode)
        ? options.effectiveMemoryMode
        : 'full',
      memoryToolsAvailable: options.memoryToolsAvailable !== false,
      memoryUnavailableReason: typeof options.memoryUnavailableReason === 'string'
        ? options.memoryUnavailableReason
        : null,
      webSearchPolicy: ['required', 'available', 'disabled'].includes(options.webSearchPolicy)
        ? options.webSearchPolicy
        : (options.forceWebSearch === true ? 'required' : 'available'),
      preferredExecutionAgentId: typeof options.preferredExecutionAgentId === 'string'
        ? options.preferredExecutionAgentId
        : null,
      currentDirectory: typeof options.currentDirectory === 'string' ? options.currentDirectory : null,
      workspaceMode: options.workspaceMode === 'worktree' ? 'worktree' : 'local',
      workspacePath: typeof options.workspacePath === 'string' ? options.workspacePath : null,
      gitFetchBeforeActions: typeof options.gitFetchBeforeActions === 'boolean' ? options.gitFetchBeforeActions : null,
      workspaceContext: null
    })
  }
}

export function emptyActivity(revision = 0) {
  return {
    phase: 'idle',
    startedAt: null,
    activeDurationMs: null,
    activeDurationUpdatedAt: null,
    activeTimingRevision: null,
    tool: null,
    toolDisplay: null,
    toolCallId: null,
    modelRequest: null,
    providerAttempt: null,
    providerAttemptTotal: null,
    index: null,
    total: null,
    revision
  }
}

export function sortSessions(sessions) {
  return [...sessions].sort(compareSessions)
}

export function compareSessions(left, right) {
  if (Boolean(left.pinned) !== Boolean(right.pinned)) return left.pinned ? -1 : 1
  const leftActivityAt = Date.parse(left.lastUserMessageAt || left.createdAt || '') || 0
  const rightActivityAt = Date.parse(right.lastUserMessageAt || right.createdAt || '') || 0
  return rightActivityAt - leftActivityAt || (left.id < right.id ? 1 : left.id > right.id ? -1 : 0)
}

// Session summaries are JSON; separate responses allocate fresh objects even for unchanged fields.
export function equalSessionSummaryValue(left, right) {
  if (left === right) return true
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object'
    || Array.isArray(left) !== Array.isArray(right)) return false
  const keys = Object.keys(right)
  return Object.keys(left).length === keys.length
    && keys.every(key => Object.hasOwn(left, key) && equalSessionSummaryValue(left[key], right[key]))
}

export function mergeSessionSummary(previous, summary) {
  let next = previous
  for (const key of Object.keys(summary)) {
    if (equalSessionSummaryValue(previous[key], summary[key])) continue
    if (next === previous) next = { ...previous }
    next[key] = summary[key]
  }
  return next
}

export function sessionScopeParams(projectId) {
  return projectId ? { projectId } : { standalone: true }
}

function lastVisibleMessageAt(messages) {
  return [...messages]
    .reverse()
    .find((message) => ['user', 'assistant'].includes(message.role))
    ?.createdAt || null
}

export function lastAssistantMessage(messages) {
  return [...(messages || [])].reverse().find((message) => message.role === 'assistant')
}

export function sessionBeforeUserMessage(session, messageId) {
  const index = session.messages?.findIndex((message) => message.id === messageId) ?? -1
  if (index < 0) return session
  const messages = session.messages.slice(0, index)
  const removedIds = new Set(session.messages.slice(index).map((message) => message.id))
  const contextSummaries = (session.contextSummaries || []).filter((summary) => (
    !removedIds.has(summary.coveredThroughMessageId)
  ))
  return {
    ...session,
    messages,
    contextSummaries,
    activeRun: null,
    lastMessageAt: lastVisibleMessageAt(messages),
    lastUserMessageAt: [...messages].reverse().find((message) => message.role === 'user')?.createdAt || null
  }
}

export function loadDrafts(ownerUserId) {
  if (!ownerUserId || typeof window === 'undefined') return emptyDrafts()
  try {
    const stored = JSON.parse(window.localStorage.getItem(DRAFT_STORAGE_KEY) || '{}')
    if (stored.ownerUserId !== ownerUserId) return emptyDrafts()
    const sessionMessages = Object.fromEntries(
      Object.entries(stored.sessionMessages || {}).filter(([, value]) => typeof value === 'string')
    )
    const sessionAgentProfiles = Object.fromEntries(
      Object.entries(stored.sessionAgentProfiles || {}).filter(([, value]) => typeof value === 'string')
    )
    const newSessions = Object.fromEntries(
      Object.entries(stored.newSessions || {}).map(([key, value]) => [
        key,
        normalizedNewSessionDraft(value)
      ])
    )
    const newSession = newSessions.standalone || normalizedNewSessionDraft(stored.newSession)
    newSessions.standalone = newSession
    return {
      newSession,
      newSessions,
      sessionMessages,
      sessionAgentProfiles
    }
  } catch {
    return emptyDrafts()
  }
}

export function persistDrafts(ownerUserId, drafts) {
  window.localStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify({ ownerUserId, ...drafts }))
}

export function clearPersistedDrafts() {
  window.localStorage.removeItem(DRAFT_STORAGE_KEY)
}
