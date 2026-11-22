function chatPanels(messages, children) {
  const transcript = { value: { messages } }
  const subagents = { children }
  const childrenByMessage = computed(() => {
    const result = new Map()
    for (const message of transcript.value.messages) {
      const children = subagents.children.filter((child) => (message.workHistory?.entries || []).some((entry) => (
        entry.tool?.id === child.spawnedByToolCallId && entry.runId === child.spawnedByRunId
      )))
      if (children.length) result.set(message.id, children)
    }
    return result
  })
  return messages.map((message) => ({
    id: message.id,
    history: message.workHistory,
    subagents: childrenByMessage.value.get(message.id) || []
  }))
}
