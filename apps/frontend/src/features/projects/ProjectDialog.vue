<template>
  <q-dialog v-model="open" :transition-duration="0" :maximized="$q.screen.lt.sm" :persistent="saving">
    <q-card class="integration-dialog app-surface column">
      <q-form class="col column no-wrap" @submit.prevent="save">
        <q-card-section>
          <div class="text-h6">{{ project ? 'Projekt bearbeiten' : 'Projekt erstellen' }}</div>
        </q-card-section>
        <q-card-section class="col scroll q-pt-none">
          <q-input dense
            v-model="name"
            outlined
            autofocus
            label="Name"
            maxlength="240"
            lazy-rules
            :rules="[requiredRule]"
          />
          <q-input dense
            v-model="instructions"
            outlined
            type="textarea"
            rows="6"
            label="Projektanweisungen"
            hint="Optionale feste Regeln und Entscheidungen für alle Sessions. Die Projekthistorie wird zusätzlich über relevante Session-Auszüge berücksichtigt."
            maxlength="4000"
            counter
          />
          <q-separator class="q-my-lg" />
          <div class="text-subtitle1 text-weight-bold q-mb-sm">Session-Vorgaben</div>
          <AppSelect dense
            v-model="sessionTemplateId"
            outlined
            emit-value
            map-options
            label="Standard-Sessionvorlage"
            :options="templateOptions"
            :loading="templates.loading"
            class="q-mb-md"
          />
          <AppSelect dense
            v-model="executionAgentId"
            outlined
            emit-value
            map-options
            label="Computer"
            :options="executionAgentOptions"
            :loading="executionAgents.loading"
            @update:model-value="selectExecutionAgent"
          />
          <q-input dense
            v-model="currentDirectory"
            outlined
            class="q-mt-md"
            label="Arbeitsverzeichnis"
            maxlength="4096"
            :disable="!selectedAgent?.connected"
            hint="Wird für neue Sessions übernommen und kann dort geändert werden."
          >
            <template #append>
              <q-btn
                flat
                round
                dense
                icon="folder_open"
                aria-label="Arbeitsverzeichnis auswählen"
                :disable="!directoryPickerAvailable"
                @click="directoryPickerOpen = true"
              >
                <q-tooltip>Arbeitsverzeichnis auswählen</q-tooltip>
              </q-btn>
            </template>
          </q-input>
          <div v-if="executionAgentId && currentDirectory" class="row items-center q-mt-sm">
            <div class="col text-caption app-muted" role="status">
              {{ !selectedAgent?.connected ? 'Computer offline; Git-Status nicht geprüft.' : !repositoryAvailable ? 'Git-Prüfung nicht verfügbar. Agent-Version und Git-Installation prüfen.' : repositoryError ? apiErrorMessage(repositoryError) : repository?.repository ? 'Git-Repository erkannt.' : repositoryLoading ? 'Git wird geprüft …' : repository ? 'Kein Git-Repository erkannt.' : 'Git-Status nicht geprüft.' }}
            </div>
            <q-btn flat round dense icon="refresh" aria-label="Git-Repository erneut prüfen" :loading="repositoryLoading" :disable="!repositoryAvailable" @click="refreshRepository" />
          </div>
          <template v-if="repository?.repository || (!repository && project?.gitTargetBranch)">
            <q-separator class="q-my-lg" />
            <div class="text-subtitle1 text-weight-bold q-mb-sm">Git</div>
            <AppSelect
              v-model="gitTargetBranch"
              dense
              outlined
              clearable
              use-input
              bottom-slots
              :input-debounce="0"
              label="Standard-Zielbranch"
              :options="branchOptions"
              :loading="repositoryLoading"
              :disable="!selectedAgent?.connected || !repository?.repository || repositoryLoading || Boolean(repositoryError)"
              class="q-mb-md"
              @filter="filterBranches"
            >
              <template #hint>
                Ausgangspunkt neuer Worktrees und Ziel für Übernahme oder Pull Request.
                <span v-if="repository?.localBranchesTruncated">Angezeigt werden die ersten 1.000 lokalen Branches und der gewählte Zielbranch.</span>
              </template>
            </AppSelect>
            <AppSelect v-model="gitWorkspaceMode" dense outlined emit-value map-options label="Standard-Arbeitsbereich" :options="[{ label: 'Lokal', value: 'local' }, { label: 'Worktree', value: 'worktree' }]" hint="Vorgabe für neue Sessions; vor dem Sessionstart änderbar." />
          </template>
        </q-card-section>
        <q-card-actions align="right" class="q-pa-md q-pt-none">
          <q-btn flat no-caps label="Abbrechen" :disable="saving" v-close-popup />
          <q-btn unelevated no-caps color="primary" label="Speichern" type="submit" :loading="saving" />
        </q-card-actions>
      </q-form>
    </q-card>
  </q-dialog>

  <ExecutionAgentDirectoryPickerDialog
    v-model="directoryPickerOpen"
    :execution-agent-id="executionAgentId"
    :initial-path="currentDirectory || selectedAgent?.homeDirectory || '/'"
    @select="currentDirectory = $event"
  />
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useProjectStore } from 'stores/projects'
import { useExecutionAgentStore } from 'stores/execution-agents'
import { useSessionTemplateStore } from 'src/features/settings/session-templates-store'
import ExecutionAgentDirectoryPickerDialog from 'src/features/computers/ExecutionAgentDirectoryPickerDialog.vue'
import { agentDisplayName } from 'src/features/computers/display'
import { useRepositoryProbe } from 'src/features/coding/repository'
import { apiErrorMessage } from 'src/utils/api-errors'

const props = defineProps({ project: { type: Object, default: null } })
const emit = defineEmits(['created'])
const open = defineModel({ type: Boolean, required: true })
const $q = useQuasar()
const projects = useProjectStore()
const executionAgents = useExecutionAgentStore()
const templates = useSessionTemplateStore()
const sessionTemplateId = ref(null)
const templateOptions = computed(() => {
  const options = [
    { label: 'Keine Projektvorgabe', value: null },
    ...templates.templates.map(item => ({ label: `${item.name}${item.scope === 'personal' ? ' (Persönlich)' : ''}`, value: item.id }))
  ]
  if (sessionTemplateId.value && !options.some(item => item.value === sessionTemplateId.value)) {
    options.push({ label: 'Nicht verfügbare Vorlage', value: sessionTemplateId.value, disable: true })
  }
  return options
})
const name = ref('')
const instructions = ref('')
const executionAgentId = ref(null)
const currentDirectory = ref(null)
const gitTargetBranch = ref(null)
const gitWorkspaceMode = ref('local')
const { repository, loading: repositoryLoading, error: repositoryError, refresh: refreshRepository } = useRepositoryProbe(executionAgentId, currentDirectory, open)
const branchSearch = ref('')
const branchOptions = computed(() => [...new Set([
  ...(repository.value?.localBranches || []),
  ...(gitTargetBranch.value ? [gitTargetBranch.value] : [])
])].filter(branch => branch.toLocaleLowerCase().includes(branchSearch.value.toLocaleLowerCase())))
function filterBranches(value, update) { update(() => { branchSearch.value = value }) }
watch(repository, (info) => {
  if (!info?.repository) return
  if (!gitTargetBranch.value) gitTargetBranch.value = info.defaultBranch
})
const directoryPickerOpen = ref(false)
const saving = ref(false)
const selectedAgent = computed(() => executionAgents.byId(executionAgentId.value))
const repositoryAvailable = computed(() => selectedAgent.value?.connected === true
  && selectedAgent.value.operations?.includes('git.repository') === true)
const executionAgentOptions = computed(() => {
  const options = [
    { label: 'Keine Projektvorgabe', value: null, connected: null, caption: 'Allgemeine Session-Vorgabe verwenden' },
    ...executionAgents.items.map(agent => ({
      label: agentDisplayName(agent), value: agent.id, connected: agent.connected,
      disable: !agent.connected && agent.id !== executionAgentId.value,
      caption: agent.connected ? agent.computerName || agent.platform : 'Offline'
    }))
  ]
  if (executionAgentId.value && !selectedAgent.value) {
    options.push({ label: 'Nicht verfügbarer Computer', value: executionAgentId.value, connected: false, caption: 'Nicht mehr eingerichtet' })
  }
  return options.map((option) => ({ ...option, icon: option.connected === false ? 'computer_off' : 'computer' }))
})
const directoryPickerAvailable = computed(() => selectedAgent.value?.connected === true
  && selectedAgent.value.operations?.includes('filesystem.list'))
const requiredRule = value => Boolean(String(value || '').trim()) || 'Pflichtfeld'

watch(open, (shown) => {
  if (!shown) return
  name.value = props.project?.name || ''
  instructions.value = props.project?.instructions || ''
  sessionTemplateId.value = props.project?.defaultSessionTemplateId || null
  executionAgentId.value = props.project?.defaultExecutionAgentId || null
  currentDirectory.value = props.project?.defaultCurrentDirectory || null
  gitTargetBranch.value = props.project?.gitTargetBranch || null
  branchSearch.value = ''
  gitWorkspaceMode.value = props.project?.gitWorkspaceMode || 'local'
  directoryPickerOpen.value = false
  void executionAgents.fetchAll({ suppressErrorToast: true }).catch(() => {})
  void templates.load().catch(() => {})
}, { immediate: true })

function selectExecutionAgent(id) {
  directoryPickerOpen.value = false
  currentDirectory.value = executionAgents.byId(id)?.homeDirectory || null
}

async function save() {
  const title = name.value.trim()
  if (!title || saving.value) return
  const defaults = {
    defaultSessionTemplateId: sessionTemplateId.value,
    defaultExecutionAgentId: executionAgentId.value,
    defaultCurrentDirectory: executionAgentId.value ? currentDirectory.value?.trim() || null : null,
    gitTargetBranch: gitTargetBranch.value || null,
    gitWorkspaceMode: gitWorkspaceMode.value
  }
  saving.value = true
  try {
    if (props.project) {
      await projects.update(props.project.id, title, instructions.value.trim(), defaults)
      open.value = false
      $q.notify({ type: 'positive', message: 'Projekt gespeichert.' })
    } else {
      const project = await projects.create(title, instructions.value.trim(), defaults)
      open.value = false
      $q.notify({ type: 'positive', message: 'Projekt erstellt.' })
      emit('created', project)
    }
  } catch {
    // The central API interceptor displays the error; the dialog remains open.
  } finally {
    saving.value = false
  }
}
</script>
