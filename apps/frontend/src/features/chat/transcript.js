import { runWarningEntries } from 'src/utils/agent-activity'
import { toolFileActions, toolOpenFileActions } from 'src/utils/attachments'
import { apiErrorMessage } from 'src/utils/api-errors'

export function retryAvailableForMessage(message, messages = [], running = false) {
  return message?.kind === 'runtime_error'
    && message.retryAvailable === true
    && messages.at(-1)?.id === message.id
    && !running
}

export function withLiveTranscriptMessage(messages, liveMessage) {
  const lastUserIndex = messages.reduce((found, message, index) => (
    message.role === 'user' && !message.continuationRunId ? index : found
  ), -1)
  const currentIndex = messages.reduce((found, message, index) => (
    message.role === 'assistant' && index > lastUserIndex ? index : found
  ), -1)
  if (liveMessage.content) {
    const displayed = [...messages]
    if (currentIndex >= 0 && displayed[currentIndex].continuationSegment) {
      displayed[currentIndex] = {
        ...displayed[currentIndex],
        content: `${displayed[currentIndex].content}\n\n${liveMessage.content}`,
        streaming: true,
        liveMessageId: liveMessage.id
      }
      return displayed
    }
    if (currentIndex >= 0 && !displayed[currentIndex].agentProfileId && liveMessage.agentProfileId) {
      displayed[currentIndex] = { ...displayed[currentIndex], agentProfileId: liveMessage.agentProfileId }
    }
    if (currentIndex >= 0 && displayed[currentIndex].currentTurn) {
      const { fileChanges, providedFiles } = displayed[currentIndex]
      liveMessage = { ...liveMessage, fileChanges, providedFiles }
      displayed[currentIndex] = { ...displayed[currentIndex], fileChanges: [], providedFiles: [], continuesAssistantTurn: true }
    }
    return [...displayed, liveMessage]
  }
  if (currentIndex >= 0) {
    const displayed = [...messages]
    displayed[currentIndex] = {
      ...displayed[currentIndex],
      ...(liveMessage.workHistory ? { workHistory: liveMessage.workHistory } : {}),
      ...(!displayed[currentIndex].agentProfileId && liveMessage.agentProfileId
        ? { agentProfileId: liveMessage.agentProfileId }
        : {}),
      liveMessageId: liveMessage.id
    }
    return displayed
  }
  return liveMessage.workHistory ? [...messages, liveMessage] : messages
}

export function withQueuedTranscriptMessages(messages, queuedMessages, sendingId) {
  const sentIds = new Set(messages.map((message) => message.id))
  const pausedEntries = new Map(queuedMessages.filter((entry) => entry.paused)
    .map((entry) => [entry.sentMessageId || entry.id, entry.id]))
  return [...messages.map((message) => pausedEntries.has(message.id)
    ? { ...message, queueRecoveryId: pausedEntries.get(message.id) } : message), ...queuedMessages
    .filter((entry) => !sentIds.has(entry.sentMessageId || entry.id))
    .map((entry) => ({
      id: entry.id,
      outboxId: entry.id,
      role: 'user',
      content: entry.content,
      attachments: entry.attachments,
      deliveryStatus: entry.id === sendingId ? 'sending' : entry.paused ? 'failed' : 'queued'
    }))]
}

export function groupWorkHistoryEntries(entries = [], current = null, includePending = false) {
  const grouped = []
  for (const entry of entries) {
    if (entry.type !== 'tool') {
      grouped.push(entry)
      continue
    }
    const previous = grouped.at(-1)
    const completed = includePending || entry.result || entry.completed || entry === current
    if (completed && previous?.type === 'tool'
      && (includePending || previous.result || previous.completed) && previous.tool?.name === entry.tool?.name) {
      grouped.splice(-1, 1, {
        key: `tool-group-${previous.key}`,
        type: 'tool-group',
        tool: previous.tool,
        items: [previous, entry],
        ...(entry.active ? { active: true } : {})
      })
      continue
    }
    if (completed && previous?.type === 'tool-group' && previous.tool?.name === entry.tool?.name) {
      previous.items.push(entry)
      previous.active ||= Boolean(entry.active)
      continue
    }
    grouped.push(entry)
  }
  return grouped
}

export function buildChatTranscript(
  messages = [],
  approvals = [],
  {
    running = false,
    runId = null,
    toolExecutionActive = running,
    activeToolCallId = null,
    completedToolCallId = null,
    processOutputs = {},
    questions = [],
    contextSummaries = []
  } = {}
) {
  const results = new Map()
  for (const message of messages) {
    if (message.role !== 'tool' || !message.toolCallId) continue
    const matches = results.get(message.toolCallId) || []
    matches.push(message)
    results.set(message.toolCallId, matches)
  }

  const approvalsByToolCall = new Map()
  for (const approval of approvals) {
    const toolCallId = approval.details?.toolCallId
    if (toolCallId) approvalsByToolCall.set(`${approval.runId || ''}:${toolCallId}`, approval)
  }

  const questionsByToolCall = new Map(questions.map((question) => [`${question.runId || ''}:${question.toolCallId}`, question]))

  let lastUserIndex = -1
  messages.forEach((message, index) => {
    if (message.role === 'user' && !(running && message.continuationRunId === runId)) lastUserIndex = index
  })

  const matchedApprovalIds = new Set()
  const matchedQuestionIds = new Set()
  let activeAssigned = false
  let executionBlocked = false
  const transcriptMessages = messages
    .map((message, index) => ({ message, index }))
    .filter(({ message }) => message.role !== 'tool')
    .map(({ message, index }) => ({
      ...message,
      content: message.error ? apiErrorMessage(message.error) : message.content,
      _sourceIndex: index,
      toolCalls: (message.toolCalls || []).map((tool, toolIndex) => {
        const currentRound = running && index > lastUserIndex
        const result = results.get(tool.id)?.shift() || null
        const toolRunId = message.runId || result?.runId || (currentRound ? runId : null)
        const requestKey = `${toolRunId || ''}:${tool.id}`
        const approval = currentRound ? approvalsByToolCall.get(requestKey) || null : null
        const question = currentRound ? questionsByToolCall.get(requestKey) || null : null
        if (approval) matchedApprovalIds.add(approval.id)
        if (question) matchedQuestionIds.add(question.id)
        const active = currentRound && toolExecutionActive && !result && !approval && !question && (activeToolCallId
          ? tool.id === activeToolCallId
          : !activeAssigned && !executionBlocked)
        if (active) activeAssigned = true
        if (currentRound && toolExecutionActive && !result && !activeToolCallId) executionBlocked = true
        return {
          key: `${message.id || index}:${tool.id || toolIndex}:${toolIndex}`,
          batchId: message.id || index,
          tool,
          ...(toolRunId ? { runId: toolRunId } : {}),
          result,
          approval,
          question,
          current: currentRound,
          active,
          completed: currentRound && tool.id === completedToolCallId,
          ...(processOutputs[tool.id] ? { processOutput: processOutputs[tool.id] } : {})
        }
      }),
      workHistory: null
    }))
  const latestUserTime = Date.parse(messages[lastUserIndex]?.createdAt || '')
  const summaryMessages = contextSummaries.map((summary) => {
    const summaryTime = Date.parse(summary.createdAt || '')
    return {
      id: `context-summary-${summary.id}`,
      role: 'assistant',
      kind: 'context_summary',
      content: '',
      toolCalls: [],
      reasoning: null,
      createdAt: summary.createdAt,
      coveredThroughMessageId: summary.coveredThroughMessageId,
      contextSummaryEntry: {
        key: `context-summary-${summary.id}`,
        type: 'context-summary',
        text: summary.content,
        warning: summary.isFallback,
        current: running
          && Number.isFinite(summaryTime)
          && Number.isFinite(latestUserTime)
          && summaryTime >= latestUserTime,
        presentation: {
          icon: 'compress',
          title: 'Gesprächskontext verdichtet',
          summary: summary.isFallback ? 'Vereinfachte Ersatzfassung' : null,
          duration: null
        }
      },
      _sourceIndex: -1,
      _timelineId: summary.id
    }
  }).sort((left, right) => {
    const leftTime = Date.parse(left.createdAt || '')
    const rightTime = Date.parse(right.createdAt || '')
    return leftTime - rightTime || left.id.localeCompare(right.id)
  })
  for (const summary of summaryMessages) {
    const summaryTime = Date.parse(summary.createdAt || '')
    const index = transcriptMessages.findIndex((message) => {
      const messageTime = Date.parse(message.createdAt || '')
      if (!Number.isFinite(summaryTime) || !Number.isFinite(messageTime)) return false
      const messageId = String(message._timelineId || message.id)
      return summaryTime < messageTime
        || (summaryTime === messageTime && summary._timelineId < messageId)
    })
    if (index < 0) transcriptMessages.push(summary)
    else transcriptMessages.splice(index, 0, summary)
  }

  const groupedMessages = []
  let assistantSection = []
  let turnStartedAt = null

  function flushAssistantSection() {
    if (!assistantSection.length) return
    const reversedSection = [...assistantSection].reverse()
    const modelResponses = reversedSection.filter((message) => message.kind !== 'runtime_error')
    const lastResponse = modelResponses.find((message) => message.content || message.toolCalls.length)
    const manuallyStopped = reversedSection.some((message) => (
      message.generationStatus === 'interrupted' && !message.redirected
    ))
    const finalAnswer = manuallyStopped
      ? modelResponses.find((message) => message.content) || null
      : lastResponse?.content && !lastResponse.toolCalls.length ? lastResponse : null
    if (finalAnswer && !finalAnswer.continuationSegment) {
      const partial = assistantSection.filter((message) => message.continuationSegment && message.content)
      if (partial.length) {
        finalAnswer.content = `${partial.map((message) => message.content).join('\n\n')}\n\n${finalAnswer.content}`
      }
    }
    const workEntries = assistantSection.flatMap((message) => [
      ...(message.contextSummaryEntry ? [message.contextSummaryEntry] : []),
      ...(message.reasoning
        ? [{ key: `reasoning-${message.id || message._sourceIndex}`, type: 'reasoning', reasoning: message.reasoning }]
        : []),
      ...runWarningEntries(message.warnings, message.id || message._sourceIndex),
      ...(message.content && message.kind !== 'runtime_error' && message !== finalAnswer && !message.continuationSegment
        ? [{
            key: `commentary-${message.id || message._sourceIndex}`,
            type: 'commentary',
            toolCallIds: message.toolCalls.map((item) => item.tool.id),
            message: {
              id: message.id,
              content: message.content,
              research: message.research,
              streaming: message.streaming,
              attachments: message.attachments
            }
          }]
        : []),
      ...message.toolCalls.map((item) => ({ ...item, type: 'tool' }))
    ])
    const reasoningItems = workEntries
      .filter((entry) => entry.type === 'reasoning')
      .map((entry) => entry.reasoning)
    const carrier = assistantSection.find((message) => message.agentProfileId) || assistantSection[0]
    carrier.preparedReply = [...workEntries].reverse().find((entry) => (
      entry.type === 'tool' && entry.result?.preparedReply
    ))?.result.preparedReply || null
    const finalCreatedAt = reversedSection.find((message) => message.createdAt)?.createdAt || null
    const activeDurationMs = reversedSection.find((message) => (
      Number.isInteger(message.activeDurationMs) && message.activeDurationMs >= 0
    ))?.activeDurationMs ?? null
    const currentTurn = running && assistantSection.some((message) => (
      message._sourceIndex > lastUserIndex || message.contextSummaryEntry?.current
    ))
    if (workEntries.length || Number.isInteger(activeDurationMs)) {
      const durations = reasoningItems.map((item) => item.observedDurationMs).filter(Number.isFinite)
      carrier.workHistory = {
        id: `work-history-${carrier.id}`,
        entries: workEntries,
        observedDurationMs: durations.length ? durations.reduce((total, value) => total + value, 0) : null,
        activeDurationMs,
        activeDurationUpdatedAt: null,
        startedAt: turnStartedAt,
        completedAt: currentTurn ? null : finalCreatedAt
      }
    }
    const response = finalAnswer || carrier
    response.fileActions = workEntries.flatMap((entry) => (
      entry.type === 'tool' ? toolOpenFileActions(entry.tool, entry.result) : []
    ))
    const trustLevels = assistantSection.map((message) => message.trustLevel)
    const effectiveTrust = trustLevels.includes('private')
      ? 'private'
      : trustLevels.includes('public') ? 'public' : null
    const finalModelName = reversedSection.find((message) => message.modelName)?.modelName || null
    const hasContent = assistantSection.some((message) => Boolean(message.content))

    for (const message of assistantSection) {
      if (message !== finalAnswer && message.kind !== 'runtime_error') {
        message.content = ''
        message.attachments = []
      }
      message.toolCalls = []
      message.reasoning = null
      message.createdAt = null
      message.modelName = null
      message.trustLevel = null
      message.activeDurationMs = null
    }
    const redirectedEmpty = assistantSection.some((message) => message.redirected)
      && !hasContent
      && !assistantSection.some((message) => message.attachments?.length)
      && workEntries.every((entry) => entry.type === 'reasoning')
    const visibleMessages = (redirectedEmpty ? [] : assistantSection).filter((message) => (
      message.kind === 'runtime_error'
      || message.content
      || message.generationStatus === 'interrupted'
      || message.attachments?.length
      || message.workHistory
    ))
    if (visibleMessages.length) {
      const finalMessage = visibleMessages.at(-1)
      finalMessage.fileChanges = response.fileActions.filter((action) => action.operation !== 'provide')
      finalMessage.providedFiles = workEntries.flatMap((entry) => entry.type === 'tool'
        ? toolFileActions(entry.result).filter((action) => action.operation === 'provide')
        : [])
      visibleMessages[0].showAgentLabel = true
      visibleMessages.forEach((message, index) => {
        message.currentTurn = currentTurn
        message.continuesAssistantTurn = index < visibleMessages.length - 1
      })
      if (!currentTurn) {
        finalMessage.createdAt = finalCreatedAt
        finalMessage.modelName = hasContent ? finalModelName : null
        finalMessage.trustLevel = effectiveTrust
        finalMessage.hasAssistantContent = hasContent
        finalMessage.activeDurationMs = workEntries.length ? null : activeDurationMs
      }
      groupedMessages.push(...visibleMessages)
    }
    for (const message of assistantSection) {
      delete message._sourceIndex
      delete message._timelineId
    }
    assistantSection = []
  }

  for (const message of transcriptMessages) {
    if (message.role === 'assistant') {
      if (message.sourceType === 'automationRun') flushAssistantSection()
      assistantSection.push(message)
      continue
    }
    if (
      message.role === 'user' && message.continuationRunId
      && assistantSection.some((entry) => entry.runId === message.continuationRunId)
    ) {
      delete message._sourceIndex
      groupedMessages.push(message)
      continue
    }
    flushAssistantSection()
    delete message._sourceIndex
    delete message._timelineId
    groupedMessages.push(message)
    if (message.role === 'user') turnStartedAt = message.createdAt || null
  }
  flushAssistantSection()

  return {
    messages: groupedMessages,
    unmatchedApprovals: approvals.filter((approval) => !matchedApprovalIds.has(approval.id)),
    unmatchedQuestions: questions.filter((question) => !matchedQuestionIds.has(question.id))
  }
}
