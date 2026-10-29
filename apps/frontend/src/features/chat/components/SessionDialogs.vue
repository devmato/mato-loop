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

  <q-dialog v-model="moveOpen" :transition-duration="0">
    <q-card class="session-rename-dialog app-surface">
      <q-form @submit.prevent="emit('move')">
        <q-card-section>
          <div class="text-h6">Session verschieben</div>
        </q-card-section>
        <q-card-section class="q-pt-none">
          <AppSelect dense
            v-model="moveProjectId"
            outlined
            emit-value
            map-options
            label="Ziel"
            :options="projectOptions"
          />
          <q-banner
            v-if="moveSourceProjectId && moveProjectId !== moveSourceProjectId"
            dense
            rounded
            class="app-surface-tonal q-mt-md"
          >
            Die Dateien bleiben dieser Session zugeordnet. Dateiverweise zwischen Sessions verhindern das Verschieben aus dem Projekt.
          </q-banner>
          <q-banner
            v-if="!moveSourceProjectId && moveProjectId"
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
            label="Verschieben"
            type="submit"
            :loading="movingSession"
            :disable="moveProjectId === moveSourceProjectId"
          />
        </q-card-actions>
      </q-form>
    </q-card>
  </q-dialog>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
defineProps({
  renaming: Boolean,
  movingSession: Boolean,
  moveSourceProjectId: { type: String, default: null },
  projectOptions: { type: Array, required: true },
  requiredRule: { type: Function, required: true }
})
const emit = defineEmits(['rename', 'move'])
const renameOpen = defineModel('renameOpen', { type: Boolean, required: true })
const renameTitle = defineModel('renameTitle', { type: String, required: true })
const moveOpen = defineModel('moveOpen', { type: Boolean, required: true })
const moveProjectId = defineModel('moveProjectId', { type: String, default: null })
</script>
