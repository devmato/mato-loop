import { runWarningEntries } from 'src/features/chat/agent-activity'
import { toolFileActions, toolOpenFileActions } from 'src/features/files/attachments'
import { apiErrorMessage } from 'src/utils/api-errors'

export function retryAvailableForMessage(message, messages = [], running = false) {
  return message?.kind === 'runtime_error'
    && message.retryAvailable === true
    && messages.at(-1)?.id === message.id
    && !running
}

export function withQueuedTranscriptMessages(messages, queuedMessages, sendingId, pendingId) {
  const sentIds = new Set(messages.flatMap((message) => [message.id, message.requestId]))
  const pausedEntries = new Map(queuedMessages.filter((entry) => entry.paused)
    .map((entry) => [entry.sentMessageId || entry.id, entry.id]))
  const unconfirmedIds = new Set(queuedMessages.filter((entry) => entry.deliveryUnconfirmed).map((entry) => entry.id))
  return [...messages.map((message) => {
    const unconfirmedId = unconfirmedIds.has(message.id) ? message.id : message.requestId
    if (unconfirmedIds.has(unconfirmedId)) return { ...message, outboxId: unconfirmedId }
    return pausedEntries.has(message.id) ? { ...message, queueRecoveryId: pausedEntries.get(message.id) } : message
  }), ...queuedMessages
    .filter((entry) => !sentIds.has(entry.sentMessageId || entry.id))
    .map((entry) => ({
      id: entry.id,
      outboxId: entry.id,
      role: 'user',
      content: entry.content,
      sourceType: entry.sourceType,
      attachments: entry.attachments,
      ...(entry.deliveryUnconfirmed ? { deliveryUnconfirmed: true } : {}),
      deliveryStatus: entry.paused ? 'failed'
        : !entry.serverQueued && (entry.id === sendingId || entry.id === pendingId) ? 'sending' : 'queued'
    }))]
}

export function groupWorkHistoryEntries(entries = [], current = null, includePending = false) {
  const grouped = []
  for (const entry of entries) {
    if (entry.type !== 'tool' || entry.subagents?.length) {
      grouped.push(entry)
      continue
    }
    const previous = grouped.at(-1)
    const completed = includePending || entry.result || entry.completed || entry === current
    if (completed && previous?.type === 'tool' && !previous.subagents?.length
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
    contextSummaries = [],
    liveMessage = null
  } = {}
) {
  if (liveMessage) {
    const originIndex = liveMessage.userMessageId
      ? messages.findIndex((message) => message.id === liveMessage.userMessageId)
      : messages.findIndex((message) => message.toolCalls?.some((tool) => tool.id === activeToolCallId))
    const nextUserIndex = originIndex >= 0
      ? messages.findIndex((message, index) => index > originIndex && message.role === 'user') : -1
    if (!liveMessage.userMessageId || originIndex >= 0) {
      const index = nextUserIndex < 0 ? messages.length : nextUserIndex
      messages = [...messages.slice(0, index), liveMessage, ...messages.slice(index)]
    }
  }
  const results = new Map()
  const carriers = new Map()
  for (const message of messages) {
    if (message.role === 'assistant') {
      for (const tool of message.toolCalls || []) {
        const matches = carriers.get(tool.id) || []
        matches.push(message)
        carriers.set(tool.id, matches)
      }
    }
    if (message.role !== 'tool' || !message.toolCallId) continue
    const carrier = [...(carriers.get(message.toolCallId) || [])].reverse().find((candidate) => (
      !candidate.runId || !message.runId || candidate.runId === message.runId
    ))
    if (!carrier) continue
    if (!results.has(carrier)) results.set(carrier, new Map())
    results.get(carrier).set(message.toolCallId, message)
  }

  const approvalsByToolCall = new Map()
  for (const approval of approvals) {
    const toolCallId = approval.details?.toolCallId
    if (toolCallId) approvalsByToolCall.set(`${approval.runId || ''}:${toolCallId}`, approval)
  }

  const questionsByToolCall = new Map(questions.map((question) => [`${question.runId || ''}:${question.toolCallId}`, question]))

  let lastUserIndex = -1
  messages.forEach((message, index) => {
    if (message.role === 'user') lastUserIndex = index
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
        const currentRound = running && (index > lastUserIndex || Boolean(runId && message.runId === runId))
        const result = results.get(message)?.get(tool.id) || null
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
  let turnRenderId = null

  function flushAssistantSection() {
    if (!assistantSection.length) return
    const live = assistantSection.find((message) => message.id === liveMessage?.id)
    const liveHistory = live ? liveMessage.workHistory : null
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
    const historyCarrier = assistantSection.find((message) => message.agentProfileId) || assistantSection[0]
    const carrier = finalAnswer || historyCarrier
    carrier.agentProfileId ||= historyCarrier.agentProfileId
    carrier.preparedReply = [...workEntries].reverse().find((entry) => (
      entry.type === 'tool' && entry.result?.preparedReply
    ))?.result.preparedReply || null
    const finalCreatedAt = reversedSection.find((message) => message.createdAt)?.createdAt || null
    const activeDurationMs = reversedSection.find((message) => (
      Number.isInteger(message.activeDurationMs) && message.activeDurationMs >= 0
    ))?.activeDurationMs ?? null
    const currentTurn = running && assistantSection.some((message) => (
      message === live || message._sourceIndex > lastUserIndex || message.contextSummaryEntry?.current
        || message.toolCalls.some((tool) => tool.active || tool.approval || tool.question)
    ))
    if (workEntries.length || Number.isInteger(activeDurationMs) || liveHistory) {
      const durations = reasoningItems.map((item) => item.observedDurationMs).filter(Number.isFinite)
      carrier.workHistory = {
        id: `work-history-${turnRenderId || historyCarrier.id}`,
        entries: workEntries,
        observedDurationMs: durations.length ? durations.reduce((total, value) => total + value, 0) : null,
        activeDurationMs: liveHistory?.activeDurationMs ?? activeDurationMs,
        activeDurationUpdatedAt: liveHistory?.activeDurationUpdatedAt || null,
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
      finalMessage.renderId = `reply-${turnRenderId || finalMessage.runId || finalMessage.id}`
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
      if (message.sourceType === 'automationRun') {
        flushAssistantSection()
        turnRenderId = message.id
        turnStartedAt = message.createdAt || null
      }
      assistantSection.push(message)
      continue
    }
    flushAssistantSection()
    delete message._sourceIndex
    delete message._timelineId
    groupedMessages.push(message)
    if (message.role === 'user') {
      turnStartedAt = message.createdAt || null
      turnRenderId = message.renderId || message.id
    }
  }
  flushAssistantSection()

  const lastMessage = groupedMessages.at(-1)
  if (running && runId && lastMessage?.role === 'user' && lastMessage.continuationRunId === runId) {
    lastMessage.reserveResponseSpace = true
  }

  return {
    messages: groupedMessages,
    unmatchedApprovals: approvals.filter((approval) => !matchedApprovalIds.has(approval.id)),
    unmatchedQuestions: questions.filter((question) => !matchedQuestionIds.has(question.id))
  }
}
