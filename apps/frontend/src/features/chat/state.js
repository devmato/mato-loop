import { equalJson } from 'src/utils/api-snapshot'
import { randomUuid } from 'src/utils/random-id'

const DRAFT_STORAGE_KEY = 'matoLoopChatDrafts'
const DRAFT_AUTOSAVE_PREFIX = `${DRAFT_STORAGE_KEY}.autosave:`
const AUTOSAVE_TOKEN_LENGTH = 36 // UUID followed by a newline and the unescaped draft text.

function draftAutosavePrefix(ownerUserId) {
  return `${DRAFT_AUTOSAVE_PREFIX}${encodeURIComponent(ownerUserId)}:`
}

function draftAutosaveKeys(storage, prefix = DRAFT_AUTOSAVE_PREFIX) {
  const keys = []
  for (let index = 0; index < storage.length; index++) {
    const key = storage.key(index)
    if (key?.startsWith(prefix)) keys.push(key)
  }
  return keys
}

function draftAutosaveToken(value) {
  return value?.[AUTOSAVE_TOKEN_LENGTH] === '\n' ? value.slice(0, AUTOSAVE_TOKEN_LENGTH) : null
}

function loadDraftSnapshot(storage, ownerUserId) {
  try {
    const stored = JSON.parse(storage.getItem(DRAFT_STORAGE_KEY) || '{}')
    if (stored?.ownerUserId === ownerUserId) return stored
  } catch {
    // A missing or invalid legacy snapshot must not hide individual drafts.
  }
  return {}
}

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

export function mergeSessionSummary(previous, summary) {
  let next = previous
  for (const key of Object.keys(summary)) {
    if (equalJson(previous[key], summary[key])) continue
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
    const storage = window.localStorage
    const stored = loadDraftSnapshot(storage, ownerUserId)
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
    const absorbed = new Set(Array.isArray(stored.absorbedAutosaves) ? stored.absorbedAutosaves : [])
    const prefix = draftAutosavePrefix(ownerUserId)
    for (const key of draftAutosaveKeys(storage, prefix)) {
      const value = storage.getItem(key)
      const token = draftAutosaveToken(value)
      if (!token || absorbed.has(token)) continue
      const target = key.slice(prefix.length)
      const id = target.slice(target.indexOf(':') + 1)
      if (!id || id === '__proto__') continue
      const message = value.slice(token.length + 1)
      if (target.startsWith('session:')) {
        if (message) sessionMessages[id] = message
        else delete sessionMessages[id]
      } else if (target.startsWith('new:')) {
        if (message && !Object.hasOwn(newSessions, id)) newSessions[id] = normalizedNewSessionDraft(null)
        if (Object.hasOwn(newSessions, id)) newSessions[id].message = message
      }
    }
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

export function persistDraftMessage(ownerUserId, scope, id, message) {
  window.localStorage.setItem(`${draftAutosavePrefix(ownerUserId)}${scope}:${id}`, `${randomUuid()}\n${message}`)
}

export function persistDraftSettings(ownerUserId, drafts) {
  const storage = window.localStorage
  const stored = loadDraftSnapshot(storage, ownerUserId)
  const previous = stored.newSessions || {}
  // Keep legacy text readable but never copy individual draft text into this settings snapshot.
  const newSessions = Object.fromEntries(Object.entries(drafts.newSessions).map(([scope, draft]) => [
    scope,
    { options: draft.options, message: previous[scope]?.message ?? (scope === 'standalone' ? stored.newSession?.message : undefined) }
  ]))
  for (const [scope, draft] of Object.entries(previous)) {
    if (scope !== '__proto__' && !Object.hasOwn(newSessions, scope) && draft?.message) {
      newSessions[scope] = { message: draft.message }
    }
  }
  const snapshot = JSON.stringify({
    ownerUserId,
    newSession: { options: newSessions.standalone?.options },
    newSessions,
    sessionMessages: stored.sessionMessages,
    sessionAgentProfiles: drafts.sessionAgentProfiles,
    absorbedAutosaves: stored.absorbedAutosaves
  })
  if (snapshot !== storage.getItem(DRAFT_STORAGE_KEY)) storage.setItem(DRAFT_STORAGE_KEY, snapshot)
}

export function clearPersistedDrafts() {
  const storage = window.localStorage
  storage.removeItem(DRAFT_STORAGE_KEY)
  for (const key of draftAutosaveKeys(storage)) storage.removeItem(key)
}
