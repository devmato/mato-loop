<template>
  <AppPage title="Variablen &amp; Secrets" subtitle="Persönliche Werte benutzerweit für Agenten und Tools hinterlegen.">
    <template #actions>
      <q-btn unelevated no-caps no-wrap color="primary" icon="add" label="Variable hinzufügen" @click="openCreate" />
    </template>

    <q-banner rounded class="app-surface-tonal q-mb-md">
      <template #avatar><q-icon name="shield" color="primary" /></template>
      Secrets werden nicht angezeigt. Agenten können einen Wert nur über eine ausdrücklich bestätigte Rückfrage speichern.
    </q-banner>
    <q-banner v-if="variables.loadFailed" rounded class="app-surface-tonal q-mb-md">
      Die Variablen und Secrets konnten nicht geladen werden.
      <template #action><q-btn flat no-caps color="primary" label="Erneut laden" @click="load" /></template>
    </q-banner>

    <q-card flat class="app-surface">
      <q-list separator>
        <q-item v-if="variables.loaded && !variables.variables.length" class="q-py-lg">
          <q-item-section avatar><q-avatar color="grey-7" text-color="white" icon="key" /></q-item-section>
          <q-item-section>
            <q-item-label>Noch keine Variablen oder Secrets</q-item-label>
            <q-item-label caption>Füge einen persönlichen Wert hinzu.</q-item-label>
          </q-item-section>
        </q-item>
        <q-item v-for="variable in variables.variables" :key="variable.id" class="q-py-md">
          <q-item-section avatar top>
            <q-avatar color="primary" text-color="white" :icon="variable.secret ? 'lock' : 'data_object'" />
          </q-item-section>
          <q-item-section>
            <div class="row items-center q-gutter-sm">
              <q-item-label class="text-weight-medium">{{ variable.name }}</q-item-label>
              <q-chip dense outline :icon="variable.secret ? 'lock' : 'visibility'">{{ variable.secret ? 'Secret' : 'Variable' }}</q-chip>
            </div>
            <q-item-label v-if="variable.description" caption lines="2">{{ variable.description }}</q-item-label>
            <q-item-label caption lines="2">{{ variable.secret ? 'Wert ausgeblendet' : variable.value }}</q-item-label>
          </q-item-section>
          <q-item-section side top>
            <div class="row items-center no-wrap q-gutter-xs">
              <q-btn flat round dense icon="edit" :aria-label="`${variable.name} bearbeiten`" @click="openEdit(variable)" />
              <q-btn flat round dense color="negative" icon="delete" :aria-label="`${variable.name} löschen`" :loading="removingId === variable.id" :disable="removingId !== null" @click="remove(variable)" />
            </div>
          </q-item-section>
        </q-item>
      </q-list>
      <q-inner-loading :showing="variables.loading" />
    </q-card>

    <q-dialog v-model="dialogOpen" :transition-duration="0" :maximized="$q.screen.lt.sm" @hide="clearValue">
      <q-card class="column no-wrap" style="width: 560px; max-width: 100vw">
        <q-card-section class="row items-center no-wrap">
          <div class="text-h6">{{ selected ? 'Variable bearbeiten' : 'Variable hinzufügen' }}</div>
          <q-space />
          <q-btn flat round dense icon="close" aria-label="Dialog schließen" @click="dialogOpen = false" />
        </q-card-section>
        <q-separator />
        <q-form class="col column no-wrap" @submit="save">
          <q-card-section class="col scroll q-gutter-md">
            <q-input dense v-model="form.name" outlined autofocus label="Name" maxlength="120" lazy-rules :rules="[requiredRule]" />
            <q-input dense
              v-model="form.description"
              outlined
              autogrow
              type="textarea"
              label="Beschreibung (optional)"
              hint="Hilft dem Assistenten, den passenden Wert auszuwählen. Keine Zugangsdaten eintragen."
              maxlength="500"
            />
            <q-input dense
              v-model="form.value"
              outlined
              :type="form.secret ? 'password' : 'textarea'"
              :autogrow="!form.secret"
              :label="selected?.secret && form.secret ? 'Neuer Wert (optional)' : 'Wert'"
              :hint="selected?.secret && form.secret ? 'Leer lassen, um das gespeicherte Secret beizubehalten.' : undefined"
              :rules="selected?.secret && form.secret ? [] : [requiredRule]"
              :autocomplete="form.secret ? 'new-password' : undefined"
            />
            <q-toggle dense v-model="form.secret" color="primary" label="Als Secret schützen" />
            <q-banner v-if="form.secret" rounded class="app-surface-tonal">
              Der Wert ist nach dem Speichern nicht mehr sichtbar.
            </q-banner>
          </q-card-section>
          <q-separator />
          <q-card-actions align="right" class="q-pa-md">
            <q-btn flat no-caps label="Abbrechen" @click="dialogOpen = false" />
            <q-btn unelevated no-caps color="primary" type="submit" label="Speichern" :loading="saving" />
          </q-card-actions>
        </q-form>
      </q-card>
    </q-dialog>
  </AppPage>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useQuasar } from 'quasar'
import AppPage from 'src/shared/ui/AppPage.vue'
import { useConfirm } from 'src/composables/confirm'
import { usePersonalVariableStore } from 'stores/personal-variables'

defineOptions({ name: 'PersonalVariablesPage' })

const $q = useQuasar()
const { confirm } = useConfirm()
const variables = usePersonalVariableStore()
const dialogOpen = ref(false)
const selected = ref(null)
const saving = ref(false)
const removingId = ref(null)
const form = reactive({ name: '', description: '', value: '', secret: false })
const requiredRule = (value) => String(value || '').length > 0 || 'Pflichtfeld'

function openCreate() {
  selected.value = null
  Object.assign(form, { name: '', description: '', value: '', secret: false })
  dialogOpen.value = true
}

function openEdit(variable) {
  selected.value = variable
  Object.assign(form, { name: variable.name, description: variable.description || '', value: variable.secret ? '' : variable.value, secret: variable.secret })
  dialogOpen.value = true
}

function clearValue() {
  form.value = ''
}

async function load() {
  try { await variables.fetchVariables() } catch { /* Central API toast. */ }
}

async function save() {
  saving.value = true
  try {
    const payload = { name: form.name, description: form.description.trim() || null, value: form.value, secret: form.secret }
    if (selected.value?.secret && form.secret && !form.value) delete payload.value
    if (selected.value) await variables.updateVariable(selected.value.id, payload)
    else await variables.createVariable(payload)
    clearValue()
    dialogOpen.value = false
    $q.notify({ type: 'positive', message: selected.value ? 'Variable gespeichert' : 'Variable hinzugefügt' })
  } catch {
    // The central Axios interceptor displays the error.
  } finally {
    saving.value = false
  }
}

async function remove(variable) {
  if (!await confirm(`„${variable.name}“ dauerhaft löschen?`, 'Variable löschen?', { okLabel: 'Löschen', okColor: 'negative' })) return
  removingId.value = variable.id
  try {
    await variables.removeVariable(variable.id)
    $q.notify({ type: 'positive', message: 'Variable gelöscht' })
  } catch {
    // The central Axios interceptor displays the error.
  } finally {
    removingId.value = null
  }
}

onMounted(load)
</script>
