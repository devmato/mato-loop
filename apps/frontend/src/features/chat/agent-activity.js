import { reasoningBlocks, reasoningHistoryPresentation, reasoningStatusLabel } from 'src/utils/reasoning-display'
import { toolActivityLabel, toolSummary } from 'src/utils/tool-display'

const LABELS = {
  submitting: 'Verarbeite Anfrage',
  queued: 'Verarbeite Anfrage',
  preparing_context: 'Verarbeite Anfrage',
  compacting_context: 'Kontext verdichten',
  waiting_approval: 'Freigabe erforderlich',
  waiting_input: 'Antwort erforderlich',
  finalizing: 'Abschließen',
  reconnecting: 'Neu verbinden'
}

const WARNING_PRESENTATIONS = {
  'agent.toolCallLimitReached': {
    title: 'Tool-Limit erreicht',
    summary: 'Der Lauf wurde ohne weitere Tool-Aufrufe abgeschlossen. Im Abschlussbericht stehen erledigte und offene Aufgaben.'
  },
  'privacy.placeholderRestorationIncomplete': {
    title: 'Geschützter Wert nicht verfügbar',
    summary: 'Ein Datenschutz-Platzhalter konnte nicht wiederhergestellt werden.'
  }
}

const ACTIVITY_ICONS = {
  submitting: 'settings',
  queued: 'settings',
  preparing_context: 'settings',
  waiting_model: 'settings',
  compacting_context: 'compress',
  waiting_approval: 'pending_actions',
  waiting_input: 'help_outline',
  reconnecting: 'sync'
}

export function runWarningEntries(warnings = [], keyPrefix = 'warning', live = false) {
  return warnings.flatMap((warning, index) => {
    const code = typeof warning === 'string' ? warning : warning?.code
    const presentation = WARNING_PRESENTATIONS[code]
    if (!presentation) return []
    return [{
      key: `${keyPrefix}-${warning?.modelRequest ?? index}-${code}`,
      type: 'warning',
      warning: true,
      live,
      presentation
    }]
  })
}

export function agentActivityLabel(activity) {
  const suppliedLabel = typeof activity?.activityLabel === 'string' ? activity.activityLabel.trim() : ''
  if (suppliedLabel) return activity.activityLabel
  if (['preparing_context', 'waiting_model'].includes(activity?.phase)) {
    return 'Verarbeite Anfrage'
  }
  if (activity?.phase === 'processing_tool') {
    return toolActivityLabel({ name: activity.tool, toolDisplay: activity.toolDisplay })
  }
  return LABELS[activity?.phase] || 'Agent arbeitet'
}

export function agentActivityDisplayKey(activity) {
  if (activity?.phase === 'waiting_model') {
    return `waiting_model:${activity.modelRequest > 1 ? 'tool_results' : 'request'}`
  }
  if (activity?.phase === 'processing_tool') {
    return `processing_tool:${activity.tool || 'unknown'}:${activity.index ?? ''}:${activity.total ?? ''}`
  }
  return activity?.phase || 'idle'
}

export function mergeAgentActivity(current, details) {
  const next = { ...current, revision: current.revision + 1 }
  if (details.phase) {
    for (const key of ['tool', 'toolDisplay', 'toolCallId']) {
      next[key] = details[key] || (['preparing_context', 'waiting_model'].includes(details.phase) ? current[key] : null)
    }
    for (const key of ['modelRequest', 'providerAttempt', 'providerAttemptTotal', 'index', 'total']) {
      next[key] = Number.isInteger(details[key]) ? details[key] : null
    }
    next.phase = details.phase
    next.activityLabel = typeof details.activityLabel === 'string' ? details.activityLabel.trim() || null : null
    next.reasoningLabel = next.phase === 'waiting_model' && current.phase === 'waiting_model'
      && next.modelRequest === current.modelRequest ? current.reasoningLabel : null
    next.startedAt = (agentActivityDisplayKey(current) === agentActivityDisplayKey(next) && current.startedAt)
      || details.startedAt || new Date().toISOString()
  }
  if (
    Number.isInteger(details.activeTimingRevision) && details.activeTimingRevision >= 0
    && Number.isInteger(details.activeDurationMs) && details.activeDurationMs >= 0
    && typeof details.activeDurationRunning === 'boolean'
    && (current.activeTimingRevision == null || details.activeTimingRevision > current.activeTimingRevision)
  ) {
    next.activeTimingRevision = details.activeTimingRevision
    next.activeDurationMs = details.activeDurationMs
    next.activeDurationRunning = details.activeDurationRunning
    next.activeDurationUpdatedAt = details.activeDurationRunning ? new Date().toISOString() : null
  }
  return next
}

export function agentRunPresentation({
  running = false,
  stopping = false,
  runId = null,
  activity = null,
  streamReasoning = null,
  streamContextSummary = null,
  streamContent = '',
  transcript = null
} = {}) {
  const hidden = { showActivity: false, label: '', summary: '', announcement: '', entry: null }
  if (!running) return hidden

  const entries = (transcript?.messages || []).flatMap((message) => (
    message.workHistory?.entries || message.toolCalls || []
  ))
  const toolItems = entries.filter((entry) => entry.tool && (!runId || !entry.runId || entry.runId === runId))
  const belongsToRun = (request) => request && (!runId || request.runId === runId)
  const approval = toolItems.find((item) => belongsToRun(item.approval))?.approval
    || transcript?.unmatchedApprovals?.find(belongsToRun)
  const question = toolItems.find((item) => belongsToRun(item.question))?.question
    || transcript?.unmatchedQuestions?.find(belongsToRun)
  if (
    !stopping
    && !approval
    && !question
    && (streamContent || ['streaming', 'finalizing'].includes(activity?.phase))
  ) {
    return { ...hidden, announcement: 'Antwort' }
  }
  streamReasoning ||= entries.findLast((entry) => entry.type === 'reasoning' && entry.live)?.reasoning
  const suppliedReasoningLabel = typeof activity?.reasoningLabel === 'string'
    ? activity.reasoningLabel.trim()
    : ''
  const reasoningSummary = activity?.phase === 'waiting_model' && !streamContent
    && (streamReasoning || suppliedReasoningLabel)
    ? suppliedReasoningLabel || reasoningStatusLabel(streamReasoning) || 'Nachdenken'
    : ''
  const selectedTool = activity?.phase === 'processing_tool'
    ? toolItems.find((item) => activity.toolCallId ? item.tool.id === activity.toolCallId : item.active)
    : ['preparing_context', 'waiting_model'].includes(activity?.phase) && !reasoningSummary && !streamContextSummary?.content
      ? activity.toolCallId
        ? toolItems.find((item) => item.tool.id === activity.toolCallId)
        : toolItems.findLast((item) => item.current !== false && (item.result || item.completed))
      : null
  const argumentsRejected = selectedTool?.result?.argumentsRejected === true
  const presentationTool = argumentsRejected ? null : selectedTool?.tool || (
    ['preparing_context', 'processing_tool', 'waiting_model'].includes(activity?.phase) && activity.tool && !reasoningSummary && !streamContextSummary?.content
      ? { id: activity.toolCallId, name: activity.tool, arguments: {}, toolDisplay: activity.toolDisplay }
      : null
  )
  const label = stopping
    ? 'Anfrage abbrechen'
    : approval
      ? 'Freigabe erforderlich'
      : question
        ? 'Antwort erforderlich'
        : reasoningSummary ? 'Nachdenken' : (presentationTool ? toolActivityLabel(presentationTool) : argumentsRejected ? 'Verarbeite Anfrage' : agentActivityLabel(activity))
  const summary = activity?.phase === 'compacting_context'
    ? ''
    : approval?.title
      || question?.questions?.[0]?.question
      || (reasoningSummary && reasoningSummary !== 'Nachdenken' ? reasoningSummary : '')
      || (presentationTool ? toolSummary(presentationTool) : '')
      || ''
  let announcement = label
  if (approval) {
    announcement = approval.title
      ? `Freigabe erforderlich: ${approval.title}`
      : 'Freigabe erforderlich'
  } else if (question) {
    announcement = 'Antwort erforderlich'
  } else if (reasoningSummary) {
    announcement = 'Nachdenken'
  }
  const active = !stopping && !approval && !question && !['waiting_approval', 'waiting_input'].includes(activity?.phase)
  let entry = toolItems.find((item) => (approval && item.approval === approval) || (question && item.question === question))
  if (!entry && !stopping) {
    if (presentationTool) entry = selectedTool || { key: `activity-tool-${activity.toolCallId}`, type: 'tool', tool: presentationTool, runId }
    else if (activity?.phase === 'waiting_model' && streamReasoning) entry = {
      key: `live-reasoning-${activity.modelRequest ?? 0}`, type: 'reasoning', blocks: reasoningBlocks(streamReasoning),
      presentation: { ...reasoningHistoryPresentation(streamReasoning), title: label, summary }
    }
    else if (activity?.phase === 'compacting_context' && streamContextSummary?.content) entry = {
      key: `live-context-summary-${activity.startedAt ?? 0}`, type: 'context-summary', text: streamContextSummary.content,
      warning: streamContextSummary.isFallback,
      presentation: { title: 'Gesprächskontext wird verdichtet', icon: 'compress' }
    }
  }
  entry ||= {
    key: 'activity-status', type: 'reasoning',
    presentation: {
      title: label,
      summary,
      icon: reasoningSummary ? 'psychology' : ACTIVITY_ICONS[activity?.phase] || 'smart_toy'
    }
  }
  return { showActivity: true, label, summary, announcement, entry: { ...entry, active }, ...(presentationTool ? { tool: presentationTool } : {}) }
}

export function activityDurationMs(history, running, now = Date.now()) {
  if (!Number.isInteger(history?.activeDurationMs) || history.activeDurationMs < 0) return null
  const activeDurationUpdatedAt = Date.parse(history.activeDurationUpdatedAt || '')
  return Math.max(0, history.activeDurationMs + (
    running && Number.isFinite(activeDurationUpdatedAt) ? now - activeDurationUpdatedAt : 0
  ))
}

export function formatActivityDuration(milliseconds) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000))
  if (seconds < 60) return `${seconds} s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${seconds % 60} s`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ${Math.floor((seconds % 3600) / 60)} min`
  return `${Math.floor(seconds / 86400)} T ${Math.floor((seconds % 86400) / 3600)} h`
}
