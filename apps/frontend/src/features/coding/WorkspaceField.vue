<template>
  <AppSelect
    :model-value="available ? session.workspaceMode || 'local' : null"
    :options="options"
    :display-value="available ? session.workspaceMode === 'worktree' ? 'Worktree' : 'Lokal' : 'Nicht verfügbar'"
    :readonly="Boolean(session.id) || !available"
    :hide-dropdown-icon="Boolean(session.id) || !available"
    :disable="disabled"
    label="Arbeitsbereich" aria-label="Arbeitsbereich"
    borderless dense stack-label emit-value map-options hide-bottom-space
    class="chat-start-select q-px-sm text-body2"
    @popup-show="refresh"
    @update:model-value="$emit('update', { workspace_mode: $event })"
  >
    <template #prepend><q-icon name="workspaces" size="18px" /></template>
  </AppSelect>
</template>

<script setup>
import { computed } from 'vue'
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { useExecutionAgentStore } from 'stores/execution-agents'
import { useProjectStore } from 'stores/projects'
import { useRepositoryProbe } from './repository'
import { apiErrorMessage } from 'src/utils/api-errors'

const props = defineProps({
  session: { type: Object, required: true },
  projectId: { type: String, default: null },
  disabled: Boolean
})
defineEmits(['update'])
const agents = useExecutionAgentStore(), projects = useProjectStore()
const available = computed(() => Boolean(props.session.preferredExecutionAgentId && props.session.currentDirectory))
const project = computed(() => !props.session.id && props.projectId ? projects.items.find(item => item.id === props.projectId
  && item.defaultExecutionAgentId === props.session.preferredExecutionAgentId
  && item.defaultCurrentDirectory === props.session.currentDirectory) : null)
const { repository, loading, error, refresh } = useRepositoryProbe(
  () => props.session.preferredExecutionAgentId, () => props.session.currentDirectory,
  () => !props.session.id && available.value && Boolean(project.value),
  () => project.value?.gitTargetBranch
)
const options = computed(() => {
  const remote = project.value?.gitRemote || repository.value?.defaultRemote
  const target = project.value?.gitTargetBranch || repository.value?.defaultBranch
  const reason = !props.session.sessionProfile?.worktreesEnabled ? apiErrorMessage({ error: { code: 'sessionTemplate.worktreesDisabled' } })
    : !project.value ? 'Projekt mit passendem Computer und Repository erforderlich.'
    : !agents.byId(props.session.preferredExecutionAgentId)?.connected ? 'Der Computer ist offline.'
      : error.value ? apiErrorMessage(error.value)
      : loading.value || !repository.value ? 'Repository wird geprüft.'
        : !agents.byId(props.session.preferredExecutionAgentId)?.operations?.includes('git.worktree') ? 'Der Computer bietet keine Worktrees an.'
          : !repository.value.repository ? 'Ein Git-Repository ist erforderlich.'
            : !target ? 'Ein Zielbranch ist erforderlich.'
              : !remote && repository.value.targetHasCommit === false ? 'Der lokale Zielbranch benötigt einen Commit.' : null
  return [
    { label: 'Lokal', value: 'local' },
    { label: 'Worktree', value: 'worktree', disable: Boolean(reason), caption: reason }
  ]
})
</script>
