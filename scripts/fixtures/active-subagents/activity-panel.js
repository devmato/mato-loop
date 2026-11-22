function panelRows(history, expanded, all, current, subagents, showActiveSubagents = true) {
  const props = { subagents, showActiveSubagents }
  const historyExpanded = { value: expanded }
  const entries = { value: all }
  const currentEntries = { value: current }
  const currentBatch = { value: null }
  const displayEntries = computed(() => {
    if (historyExpanded.value) return entries.value
    const current = currentEntries.value.filter((entry) => !entry.subagents?.length || entry.subagents.some(isActiveSubagent)).map((entry) => ({
      ...entry, subagents: entry.subagents?.filter(isActiveSubagent)
    }))
    return currentBatch.value != null ? groupWorkHistoryEntries(current, null, true) : current
  })
  const activeSubagents = computed(() => {
    const visible = new Set(currentEntries.value.flatMap((entry) => (entry.subagents || []).map((child) => child.id)))
    return props.subagents.filter((child) => isActiveSubagent(child) && !visible.has(child.id))
  })
  if (!history) return { rows: [], entries: [] }
  return {
    rows: historyExpanded.value ? [] : activeSubagents.value,
    entries: displayEntries.value
  }
}
