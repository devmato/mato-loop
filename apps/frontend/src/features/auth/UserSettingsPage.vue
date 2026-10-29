<template>
  <section>
    <header class="settings-section-header">
      <div>
        <h2 class="text-h6 text-weight-bold q-my-none">Benutzer</h2>
        <p class="text-body2 app-muted q-mb-none q-mt-xs">
          Lokale Konten, Rollen und Zugänge verwalten.
        </p>
      </div>
      <q-btn color="primary" unelevated no-caps icon="person_add" label="Benutzer anlegen" @click="openCreate" />
    </header>

    <q-banner v-if="users.loadFailed" rounded class="app-surface-tonal q-mb-md">
      Die Benutzer konnten nicht geladen werden.
      <template #action>
        <q-btn flat no-caps color="primary" label="Erneut laden" @click="load" />
      </template>
    </q-banner>

    <q-card flat class="app-surface user-list-card">
      <q-list separator>
        <q-item v-for="user in users.items" :key="user.id" class="settings-list-item q-py-md">
          <q-item-section avatar top>
            <q-avatar :color="user.isActive ? 'primary' : 'grey-7'" text-color="white" icon="person" />
          </q-item-section>
          <q-item-section>
            <div class="row items-center q-gutter-sm">
              <q-item-label class="settings-list-name text-weight-medium">{{ user.username }}</q-item-label>
              <q-chip v-if="user.id === auth.userId" dense outline color="primary">Du</q-chip>
              <q-chip v-if="user.isAdmin" dense color="primary" text-color="white">Administrator</q-chip>
              <q-chip dense :color="user.isActive ? 'positive' : 'grey-7'" text-color="white">
                {{ user.isActive ? 'Aktiv' : 'Deaktiviert' }}
              </q-chip>
            </div>
            <q-item-label caption>
              {{ user.hasLocalPassword ? 'Lokales Passwort eingerichtet' : 'Kein lokales Passwort' }}
            </q-item-label>
          </q-item-section>
          <q-item-section side top class="settings-list-actions">
            <div class="row items-center no-wrap q-gutter-sm">
              <q-toggle dense
                :model-value="user.isAdmin"
                color="primary"
                label="Admin"
                :aria-label="`${user.username}: Administratorrechte`"
                :disable="Boolean(pendingUserId)"
                @update:model-value="setAdmin(user, $event)"
              />
              <div class="row items-center no-wrap q-gutter-xs">
                <q-btn
                  flat
                  round
                  dense
                  icon="password"
                  :disable="Boolean(pendingUserId)"
                  :aria-label="`Passwort für ${user.username} zurücksetzen`"
                  @click="openPassword(user)"
                >
                  <q-tooltip>Passwort zurücksetzen</q-tooltip>
                </q-btn>
                <q-btn flat round dense icon="more_vert" :aria-label="`Aktionen für ${user.username}`" :disable="Boolean(pendingUserId)" :loading="pendingUserId === user.id && pendingAction !== 'admin'">
                  <AppMenu>
                    <q-list>
                      <q-item clickable v-close-popup :disable="Boolean(pendingUserId)" @click="setActive(user)">
                        <q-item-section avatar><q-icon :name="user.isActive ? 'person_off' : 'person_add'" /></q-item-section>
                        <q-item-section>{{ user.isActive ? 'Deaktivieren' : 'Aktivieren' }}</q-item-section>
                      </q-item>
                      <q-separator />
                      <q-item clickable v-close-popup class="text-negative" :disable="Boolean(pendingUserId) || user.isActive" @click="removeUser(user)">
                        <q-item-section avatar><q-icon name="delete_outline" /></q-item-section>
                        <q-item-section>
                          <q-item-label>Löschen</q-item-label>
                          <q-item-label v-if="user.isActive" caption>Vor dem Löschen deaktivieren</q-item-label>
                        </q-item-section>
                      </q-item>
                    </q-list>
                  </AppMenu>
                </q-btn>
              </div>
            </div>
          </q-item-section>
        </q-item>
      </q-list>
      <q-inner-loading :showing="users.loading" />
    </q-card>

    <q-dialog v-model="createDialogOpen" :transition-duration="0" :maximized="$q.screen.lt.sm" :persistent="saving">
      <q-card class="user-dialog app-surface">
        <q-form @submit.prevent="createUser">
          <q-card-section>
            <div class="text-h6">Benutzer anlegen</div>
          </q-card-section>
          <q-card-section class="q-pt-none q-gutter-md">
            <q-input dense v-model="createForm.username" outlined autofocus label="Benutzername" autocomplete="off" :rules="[requiredRule]" />
            <q-input dense
              v-model="createForm.password"
              outlined
              type="password"
              label="Passwort"
              autocomplete="new-password"
              hint="Mindestens 10 Zeichen"
              :rules="[passwordRule]"
            />
            <q-input dense
              v-model="createForm.confirmation"
              outlined
              type="password"
              label="Passwort bestätigen"
              autocomplete="new-password"
              :rules="[(value) => confirmationRule(value, createForm.password)]"
            />
            <q-toggle dense v-model="createForm.isAdmin" color="primary" label="Administratorrechte" />
          </q-card-section>
          <q-card-actions align="right" class="q-pa-md q-pt-none">
            <q-btn v-close-popup flat no-caps label="Abbrechen" :disable="saving" />
            <q-btn unelevated no-caps color="primary" label="Anlegen" type="submit" :loading="saving" />
          </q-card-actions>
        </q-form>
      </q-card>
    </q-dialog>

    <q-dialog v-model="passwordDialogOpen" :transition-duration="0" :maximized="$q.screen.lt.sm" :persistent="saving">
      <q-card class="user-dialog app-surface">
        <q-form @submit.prevent="resetPassword">
          <q-card-section>
            <div class="text-h6">Passwort zurücksetzen</div>
            <div class="text-body2 app-muted q-mt-xs">{{ selectedUser?.username }}</div>
          </q-card-section>
          <q-card-section class="q-pt-none q-gutter-md">
            <q-input dense
              v-model="newPassword"
              outlined
              autofocus
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
              label="Passwort bestätigen"
              autocomplete="new-password"
              :rules="[(value) => confirmationRule(value, newPassword)]"
            />
            <q-banner rounded class="app-surface-tonal">
              Alle bestehenden Anmeldungen dieses Benutzers werden beendet.
            </q-banner>
          </q-card-section>
          <q-card-actions align="right" class="q-pa-md q-pt-none">
            <q-btn v-close-popup flat no-caps label="Abbrechen" :disable="saving" />
            <q-btn unelevated no-caps color="primary" label="Zurücksetzen" type="submit" :loading="saving" />
          </q-card-actions>
        </q-form>
      </q-card>
    </q-dialog>
  </section>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useQuasar } from 'quasar'
import AppMenu from 'src/shared/ui/AppMenu.vue'
import { useConfirm } from 'src/composables/confirm'
import { useAuthStore } from 'stores/auth'
import { useUsersStore } from 'stores/users'

defineOptions({ name: 'UserSettingsPage' })

const $q = useQuasar()
const { confirm } = useConfirm()
const auth = useAuthStore()
const users = useUsersStore()
const createDialogOpen = ref(false)
const passwordDialogOpen = ref(false)
const selectedUser = ref(null)
const newPassword = ref('')
const passwordConfirmation = ref('')
const saving = ref(false)
const pendingUserId = ref(null)
const pendingAction = ref(null)
const createForm = reactive({ username: '', password: '', confirmation: '', isAdmin: false })
const requiredRule = (value) => Boolean(String(value || '').trim()) || 'Pflichtfeld'
const passwordRule = (value) => String(value || '').length >= 10 || 'Das Passwort muss mindestens 10 Zeichen lang sein.'
const confirmationRule = (value, password) => value === password || 'Die Passwörter stimmen nicht überein.'

function openCreate() {
  Object.assign(createForm, { username: '', password: '', confirmation: '', isAdmin: false })
  createDialogOpen.value = true
}

function openPassword(user) {
  selectedUser.value = user
  newPassword.value = ''
  passwordConfirmation.value = ''
  passwordDialogOpen.value = true
}

async function createUser() {
  saving.value = true
  try {
    await users.create({
      username: createForm.username.trim(),
      password: createForm.password,
      is_admin: createForm.isAdmin
    })
    createDialogOpen.value = false
    $q.notify({ type: 'positive', message: 'Benutzer angelegt' })
  } catch {
    // The central API interceptor displays the error; the dialog remains open.
  } finally {
    saving.value = false
  }
}

async function setAdmin(user, isAdmin) {
  if (!isAdmin && !await confirm(
    `Administratorrechte von „${user.username}“ entziehen?`,
    'Administratorrechte entziehen',
    { okLabel: 'Entziehen' }
  )) return
  pendingUserId.value = user.id
  pendingAction.value = 'admin'
  try {
    await users.update(user.id, { is_admin: isAdmin })
    $q.notify({ type: 'positive', message: 'Administratorrechte aktualisiert' })
    if (user.id === auth.userId) await auth.fetchMe()
  } catch {
    // The central API interceptor displays the error; server state remains visible.
  } finally {
    pendingUserId.value = null
    pendingAction.value = null
  }
}

async function setActive(user) {
  if (user.isActive && !await confirm(
    `„${user.username}“ deaktivieren? Alle bestehenden Anmeldungen werden beendet.`,
    'Benutzer deaktivieren',
    { okLabel: 'Deaktivieren' }
  )) return
  const nextActive = !user.isActive
  pendingUserId.value = user.id
  pendingAction.value = 'active'
  try {
    await users.update(user.id, { is_active: nextActive })
    $q.notify({ type: 'positive', message: nextActive ? 'Benutzer aktiviert' : 'Benutzer deaktiviert' })
  } catch {
    // The central API interceptor displays the error; server state remains visible.
  } finally {
    pendingUserId.value = null
    pendingAction.value = null
  }
}

async function resetPassword() {
  if (!selectedUser.value) return
  saving.value = true
  try {
    await users.resetPassword(selectedUser.value.id, newPassword.value)
    passwordDialogOpen.value = false
    $q.notify({ type: 'positive', message: 'Passwort zurückgesetzt' })
    if (selectedUser.value.id === auth.userId) await auth.logout()
  } catch {
    // The central API interceptor displays the error; the dialog remains open.
  } finally {
    saving.value = false
  }
}

async function removeUser(user) {
  if (!await confirm(
    `„${user.username}“ endgültig löschen? Alle Sessions und zugehörigen Daten werden entfernt.`,
    'Benutzer löschen',
    { okLabel: 'Löschen' }
  )) return
  pendingUserId.value = user.id
  pendingAction.value = 'delete'
  try {
    await users.remove(user.id)
    $q.notify({ type: 'positive', message: 'Benutzer gelöscht' })
  } catch {
    // The central API interceptor displays the error; server state remains visible.
  } finally {
    pendingUserId.value = null
    pendingAction.value = null
  }
}

async function load() {
  try {
    await users.fetchAll()
  } catch {
    // The central API interceptor displays the error and the banner offers a retry.
  }
}

onMounted(load)
</script>
