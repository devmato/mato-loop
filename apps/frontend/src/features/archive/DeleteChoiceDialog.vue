<template>
  <q-dialog ref="dialogRef" :transition-duration="0" @hide="onDialogHide">
    <q-card style="width: 480px; max-width: 100vw">
      <q-card-section class="text-h6" role="heading" aria-level="2">{{ label }} löschen?</q-card-section>
      <q-card-section class="q-pt-none">
        <p class="text-break q-mb-none">„{{ title }}“</p>
        <div v-if="!permanentLocked" class="q-mt-sm">
          <q-checkbox v-model="permanent" label="Sofort löschen" />
        </div>
        <template v-if="session && worktree">
          <div class="q-mt-sm">
            <q-checkbox v-model="removeWorktree" :disable="Boolean(worktree.removalError && !worktree.scheduled)" label="Worktree ebenfalls entfernen" />
          </div>
          <p v-if="worktree.removalError" class="q-mb-none text-caption text-break" role="status">{{ apiErrorMessage({ error: worktree.removalError }) }}</p>
          <div v-if="!worktree.removalError || worktree.scheduled" :class="{ invisible: !removeWorktree }" :aria-hidden="!removeWorktree">
            <p class="q-mb-none text-caption" role="status">Der Worktree wird sofort und dauerhaft entfernt. Nicht committete Änderungen und unversionierte Dateien (auch ignorierte Dateien) gehen verloren. Ungesicherte Commits können verloren gehen.</p>
            <p v-if="worktree.sessions?.length" class="q-mb-none text-caption text-break">Beim Entfernen wechseln zu Lokal: {{ worktree.sessions.map(item => item.title || 'Ohne Titel').join(', ') }}.</p>
          </div>
        </template>
        <p v-else-if="session && hasWorktree" class="q-mt-sm q-mb-none text-caption" role="status">Der Worktree kann nicht geprüft werden und bleibt erhalten.</p>
      </q-card-section>
      <q-card-actions align="right" class="delete-actions">
        <q-btn flat no-caps label="Abbrechen" @click="onDialogCancel" />
        <q-btn unelevated no-caps :color="permanent ? 'negative' : 'primary'" @click="submit">
          <span class="delete-variants">
            <span :class="{ invisible: permanent }" :aria-hidden="permanent">In den Papierkorb</span>
            <span :class="{ invisible: !permanent }" :aria-hidden="!permanent">Sofort löschen</span>
          </span>
        </q-btn>
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup>
import { ref } from 'vue'
import { useDialogPluginComponent } from 'quasar'
import { apiErrorMessage } from 'src/utils/api-errors'

const props = defineProps({
  label: { type: String, required: true }, title: { type: String, required: true },
  session: Boolean, permanentLocked: Boolean,
  worktree: { type: Object, default: null }, hasWorktree: Boolean
})
defineEmits([...useDialogPluginComponent.emits])
const { dialogRef, onDialogHide, onDialogOK, onDialogCancel } = useDialogPluginComponent()
const permanent = ref(props.permanentLocked)
const removeWorktree = ref(Boolean(props.worktree?.scheduled))

function submit() {
  onDialogOK(props.session ? {
    permanent: permanent.value,
    worktreeImpact: removeWorktree.value && !props.worktree?.scheduled ? props.worktree?.impact || null : null,
    keepWorktree: Boolean(props.worktree?.scheduled && !removeWorktree.value)
  } : permanent.value)
}
</script>

<style scoped>
.delete-variants {
  display: grid;
}

.delete-variants > * {
  grid-area: 1 / 1;
}

@media (max-width: 400px) {
  .delete-actions {
    flex-direction: column;
    align-items: stretch;
    gap: 4px;
  }

  .delete-actions > .q-btn + .q-btn {
    margin-left: 0;
  }
}
</style>
