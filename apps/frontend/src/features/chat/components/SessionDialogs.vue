<template>
  <q-dialog v-model="renameOpen" :transition-duration="0">
    <q-card class="session-rename-dialog app-surface">
      <q-form @submit.prevent="emit('rename')">
        <q-card-section>
          <div class="text-h6">Session umbenennen</div>
        </q-card-section>
        <q-card-section class="q-pt-none">
          <q-input dense
            v-model="renameTitle"
            outlined
            autofocus
            label="Titel"
            maxlength="240"
            lazy-rules
            :rules="[requiredRule]"
          />
        </q-card-section>
        <q-card-actions align="right" class="q-pa-md q-pt-none">
          <q-btn flat no-caps label="Abbrechen" :disable="renaming" v-close-popup />
          <q-btn
            unelevated
            no-caps
            color="primary"
            label="Speichern"
            type="submit"
            :loading="renaming"
          />
        </q-card-actions>
      </q-form>
    </q-card>
  </q-dialog>

  <q-dialog v-model="moveOpen" :transition-duration="0" :persistent="movingSession">
    <q-card class="session-rename-dialog app-surface">
      <q-form @submit.prevent="submitMove">
        <q-card-section>
          <div class="text-h6">Session verschieben</div>
        </q-card-section>
        <q-card-section class="q-pt-none">
          <template v-if="creatingProject">
            <q-input
              ref="projectNameInput"
              v-model="projectName"
              dense
              outlined
              label="Projektname"
              maxlength="240"
              :disable="movingSession"
            />
            <q-btn flat dense no-caps color="primary" icon="arrow_back" label="Bestehendes Ziel auswählen"
              class="q-mt-xs" :disable="movingSession" @click="showExistingProjects" />
          </template>
          <template v-else>
            <AppSelect dense
              v-model="moveProjectId"
              outlined
              emit-value
              map-options
              label="Ziel"
              :options="projectOptions"
              :disable="movingSession"
            />
            <q-btn ref="newProjectAction" flat dense no-caps color="primary" icon="add" label="Neues Projekt anlegen"
              class="q-mt-xs" :disable="movingSession" @click="showNewProject" />
            <div v-if="targetProject" class="q-mt-sm">
              <q-checkbox v-model="applyProjectDefaults" dense label="Projektvorgaben übernehmen" :disable="movingSession" />
              <div v-if="applyProjectDefaults && changes.length" class="text-caption q-mt-xs" aria-live="polite">
                {{ changes.join(' ') }}
              </div>
            </div>
          </template>
          <q-banner
            v-if="moveSourceProjectId && (creatingProject || moveProjectId !== moveSourceProjectId)"
            dense
            rounded
            class="app-surface-tonal q-mt-md"
          >
            Die Dateien bleiben dieser Session zugeordnet. Dateiverweise zwischen Sessions verhindern das Verschieben aus dem Projekt.
          </q-banner>
          <q-banner
            v-if="!moveSourceProjectId && (creatingProject || moveProjectId)"
            dense
            rounded
            class="app-surface-tonal q-mt-md"
          >
            Die Dateien werden mit der Session in das Projekt verschoben und bleiben ihr zugeordnet.
          </q-banner>
        </q-card-section>
        <q-card-actions align="right" class="q-pa-md q-pt-none">
          <q-btn flat no-caps label="Abbrechen" :disable="movingSession" v-close-popup />
          <q-btn
            unelevated
            no-caps
            color="primary"
            :label="creatingProject ? 'Anlegen und verschieben' : 'Verschieben'"
            type="submit"
            :loading="movingSession"
            :disable="!canMove"
          />
        </q-card-actions>
      </q-form>
    </q-card>
  </q-dialog>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import AppSelect from 'src/shared/ui/AppSelect.vue'
const props = defineProps({
  renaming: Boolean,
  movingSession: Boolean,
  moveSourceSession: { type: Object, default: null },
  projectOptions: { type: Array, required: true },
  requiredRule: { type: Function, required: true }
})
const emit = defineEmits(['rename', 'move'])
const renameOpen = defineModel('renameOpen', { type: Boolean, required: true })
const renameTitle = defineModel('renameTitle', { type: String, required: true })
const moveOpen = defineModel('moveOpen', { type: Boolean, required: true })
const moveProjectId = defineModel('moveProjectId', { type: String, default: null })
const creatingProject = ref(false)
const projectName = ref('')
const applyProjectDefaults = ref(true)
const projectNameInput = ref(null)
const newProjectAction = ref(null)
const moveSourceProjectId = computed(() => props.moveSourceSession?.projectId || null)
const canMove = computed(() => creatingProject.value ? !!projectName.value.trim() : moveProjectId.value !== moveSourceProjectId.value)
const targetProject = computed(() => canMove.value && moveProjectId.value && !creatingProject.value
  ? props.projectOptions.find(({ value }) => value === moveProjectId.value)?.project : null)
const changes = computed(() => {
  const project = targetProject.value
  const session = props.moveSourceSession
  if (!project || !session) return []
  const details = []
  if (project.defaultSessionTemplateId) details.push('Die Session-Vorlage des Zielprojekts wird angewendet.')
  if (project.gitWorkspaceMode === 'worktree') details.push('Die Session wechselt in einen Worktree des Zielprojekts.')
  else if (project.defaultExecutionAgentId && (session.preferredExecutionAgentId !== project.defaultExecutionAgentId
    || session.currentDirectory !== project.defaultCurrentDirectory || session.workspaceMode === 'worktree')) {
    details.push(project.defaultCurrentDirectory
      ? `Neues Arbeitsverzeichnis: ${project.defaultCurrentDirectory}.`
      : 'Die Session wechselt zum Arbeitsbereich des Zielprojekts.')
  }
  if (session.workspaceMode === 'worktree' && (project.defaultExecutionAgentId || project.gitWorkspaceMode === 'worktree')) {
    details.push('Der bisherige Worktree wird nicht verändert.')
  }
  return details
})

watch(moveOpen, (open) => {
  if (!open) return
  creatingProject.value = false
  projectName.value = ''
  applyProjectDefaults.value = true
})

async function showNewProject() {
  creatingProject.value = true
  await nextTick()
  projectNameInput.value?.focus()
}

async function showExistingProjects() {
  creatingProject.value = false
  await nextTick()
  newProjectAction.value?.$el.focus()
}

function submitMove() {
  if (props.movingSession || !canMove.value) return
  emit('move', creatingProject.value
    ? { newProjectName: projectName.value.trim() }
    : { applyProjectDefaults: !!targetProject.value && applyProjectDefaults.value })
}
</script>
