<template>
  <q-dialog
    :transition-duration="0"
    :model-value="modelValue"
    :maximized="$q.screen.lt.sm"
    :persistent="saving"
    no-refocus
    aria-labelledby="account-dialog-title"
    @before-show="resetForm"
    @hide="handleHide"
  >
    <q-card class="user-dialog app-surface">
      <q-form class="column full-height" @submit.prevent="changePassword">
        <q-card-section class="row items-start no-wrap q-gutter-md">
          <q-avatar color="primary" text-color="white" icon="manage_accounts" />
          <div class="col">
            <div id="account-dialog-title" class="text-h6">Konto</div>
            <div class="text-subtitle1 text-weight-medium">{{ auth.username }}</div>
            <div class="text-body2 app-muted">{{ auth.isAdmin ? 'Administrator' : 'Benutzer' }}</div>
          </div>
          <q-btn flat round dense icon="close" aria-label="Konto schließen" :disable="saving" @click="close" />
        </q-card-section>

        <q-separator />

        <q-card-section class="col scroll q-gutter-md">
          <div class="text-subtitle1 text-weight-medium">Passwort ändern</div>
          <q-input dense
            v-model="currentPassword"
            outlined
            autofocus
            type="password"
            label="Aktuelles Passwort"
            autocomplete="current-password"
            :rules="[requiredRule]"
          />
          <q-input dense
            v-model="newPassword"
            outlined
            type="password"
            label="Neues Passwort"
            autocomplete="new-password"
            hint="Mindestens 10 Zeichen"
            :rules="[passwordRule]"
          />
          <q-input dense
            v-model="passwordConfirmation"
            outlined
            type="password"
            label="Neues Passwort bestätigen"
            autocomplete="new-password"
            :rules="[confirmationRule]"
          />
          <q-banner rounded class="app-surface-tonal">
            Nach der Änderung musst du dich mit dem neuen Passwort erneut anmelden.
          </q-banner>
        </q-card-section>

        <q-separator />
        <q-card-actions class="q-pa-md">
          <q-btn flat no-caps label="Abbrechen" :disable="saving" @click="close" />
          <q-space />
          <q-btn
            unelevated
            no-caps
            color="primary"
            type="submit"
            icon="password"
            label="Passwort ändern"
            :loading="saving"
          />
        </q-card-actions>
      </q-form>
    </q-card>
  </q-dialog>
</template>

<script setup>
import { ref } from 'vue'
import { useQuasar } from 'quasar'
import { useAuthStore } from 'stores/auth'

defineOptions({ name: 'AccountDialog' })

const props = defineProps({
  modelValue: { type: Boolean, required: true },
  beforePasswordChange: { type: Function, required: true }
})
const emit = defineEmits(['update:modelValue', 'closed'])
const $q = useQuasar()
const auth = useAuthStore()
const currentPassword = ref('')
const newPassword = ref('')
const passwordConfirmation = ref('')
const saving = ref(false)
const requiredRule = (value) => Boolean(value) || 'Pflichtfeld'
const passwordRule = (value) => String(value || '').length >= 10 || 'Das Passwort muss mindestens 10 Zeichen lang sein.'
const confirmationRule = (value) => value === newPassword.value || 'Die Passwörter stimmen nicht überein.'

function resetForm() {
  currentPassword.value = ''
  newPassword.value = ''
  passwordConfirmation.value = ''
}

function close() {
  if (saving.value) return
  emit('update:modelValue', false)
}

function handleHide() {
  resetForm()
  emit('update:modelValue', false)
  emit('closed')
}

async function changePassword() {
  if (!await props.beforePasswordChange({ discard: false })) return
  saving.value = true
  try {
    await auth.changePassword({
      current_password: currentPassword.value,
      new_password: newPassword.value
    })
  } catch {
    // The central API interceptor displays the error; the dialog remains open for correction.
  } finally {
    saving.value = false
  }
}
</script>
