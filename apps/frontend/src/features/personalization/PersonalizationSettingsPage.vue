<template>
  <section>
    <q-tabs
      v-model="tab"
      dense
      no-caps
      align="left"
      active-color="primary"
      indicator-color="primary"
      class="q-mb-lg"
    >
      <q-tab name="profile" icon="person" label="Über mich" />
      <q-tab name="assistant" icon="face" label="Personas" />
    </q-tabs>

    <q-tab-panels v-model="tab" class="bg-transparent">
      <q-tab-panel name="profile" class="q-pa-none">
        <header class="settings-section-header">
          <div>
            <div class="row items-center q-gutter-sm">
              <h2 class="text-h6 text-weight-bold q-my-none">Persönliches Profil</h2>
              <q-badge v-if="dirty" color="warning" text-color="black" label="Ungespeichert" />
            </div>
            <p class="text-body2 app-muted q-mb-none q-mt-xs">
              Persönliche Angaben für passendere Antworten. Benutzername und Kontodaten bleiben unverändert.
            </p>
          </div>
          <q-btn
            color="primary"
            unelevated
            no-caps
            icon="save"
            label="Speichern"
            :loading="profile.saving"
            :disable="profile.loading || !profile.loaded || !dirty"
            @click="save"
          />
        </header>

        <q-banner v-if="profile.loadFailed" rounded class="bg-negative text-white q-mb-md">
          <template #avatar><q-icon name="error_outline" /></template>
          Das persönliche Profil konnte nicht geladen werden.
          <template #action>
            <q-btn flat no-caps color="white" label="Erneut laden" @click="load" />
          </template>
        </q-banner>

        <q-banner v-if="auth.modelTrustEnabled" rounded class="app-surface-tonal q-mb-md">
          <template #avatar><q-icon name="privacy_tip" color="primary" /></template>
          <div class="text-subtitle1 text-weight-bold">Sichtbarkeit bewusst wählen</div>
          <div class="text-body2 app-muted q-mt-xs">
            Angaben für öffentliche Modelle können an externe Modellanbieter übertragen werden.
            „Privat“ bedeutet nicht automatisch lokale Verarbeitung. Speichere hier keine Passwörter,
            Schlüssel oder anderen vertraulichen Daten.
          </div>
        </q-banner>

        <q-card flat class="app-surface">
          <q-card-section v-if="auth.modelTrustEnabled">
            <q-btn-toggle
              v-model="profileAudience"
              spread
              no-caps
              unelevated
              toggle-color="primary"
              :options="audienceOptions"
              :disable="profile.loading || profile.saving || !profile.loaded"
            />
          </q-card-section>
          <q-separator v-if="auth.modelTrustEnabled" />
          <q-card-section>
            <q-form class="q-gutter-y-md" @submit.prevent="save">
              <div v-if="visibleFields.length" class="q-gutter-y-md">
                <UserProfileFieldEditor
                  v-for="field in visibleFields"
                  :key="field.name"
                  :field="field"
                  :model-value="fieldValue(field.name)"
                  :shared="draft[field.name].shared"
                  :audience="profileAudience"
                  :model-trust-enabled="auth.modelTrustEnabled"
                  :hint="fieldHint(field)"
                  :input-disabled="fieldDisabled(field.name)"
                  :disabled="profile.loading || profile.saving || !profile.loaded"
                  @update:model-value="setFieldValue(field.name, $event)"
                  @update:shared="draft[field.name].shared = $event"
                  @remove="removeField(field)"
                />
              </div>
              <div v-else class="text-center app-muted q-py-lg">
                <q-icon name="person_add" size="40px" />
                <div class="text-body1 q-mt-sm">Noch keine persönlichen Angaben hinterlegt.</div>
              </div>
              <q-btn
                outline
                no-caps
                color="primary"
                icon="add"
                label="Angabe hinzufügen"
                :disable="profile.loading || profile.saving || !profile.loaded || !availableFields.length"
                @click="addFieldDialogOpen = true"
              />
              <button type="submit" class="hidden" aria-hidden="true" tabindex="-1" />
            </q-form>
          </q-card-section>
        </q-card>

        <q-banner dense rounded inline-actions class="app-surface-tonal q-mt-md">
          <template #avatar><q-icon name="info_outline" color="primary" /></template>
          Weitere persönliche Angaben kannst du als Memory speichern.
          <template #action>
            <q-btn
              flat
              no-caps
              color="primary"
              label="Memory verwalten"
              :to="{ name: 'settings-memory' }"
            />
          </template>
        </q-banner>

        <q-inner-loading :showing="profile.loading" />

        <q-dialog
          :transition-duration="0"
          v-model="addFieldDialogOpen"
          :maximized="$q.screen.lt.sm"
          aria-labelledby="add-profile-field-title"
        >
          <q-card class="user-dialog app-surface">
            <q-card-section class="row items-center no-wrap">
              <div>
                <div id="add-profile-field-title" class="text-h6">Angabe hinzufügen</div>
                <div class="text-body2 app-muted">Wähle eine Angabe für dein persönliches Profil.</div>
              </div>
              <q-space />
              <q-btn flat round dense icon="close" aria-label="Dialog schließen" v-close-popup />
            </q-card-section>
            <q-separator />
            <q-list separator>
              <q-item
                v-for="field in availableFields"
                :key="field.name"
                clickable
                v-ripple
                @click="addField(field.name)"
              >
                <q-item-section avatar><q-icon :name="field.icon" color="primary" /></q-item-section>
                <q-item-section>
                  <q-item-label>{{ field.label }}</q-item-label>
                  <q-item-label caption>{{ field.description }}</q-item-label>
                </q-item-section>
              </q-item>
            </q-list>
          </q-card>
        </q-dialog>
      </q-tab-panel>

      <q-tab-panel name="assistant" class="q-pa-none">
        <AgentIdentitySettingsPage />
      </q-tab-panel>
    </q-tab-panels>
  </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import UserProfileFieldEditor from 'src/features/personalization/UserProfileFieldEditor.vue'
import AgentIdentitySettingsPage from 'src/features/personalization/AgentIdentitySettingsPage.vue'
import { useConfirm } from 'src/composables/confirm'
import { useAuthStore } from 'stores/auth'
import {
  profileFactDraft,
  profileFactPayload,
  useUserProfileStore,
  userProfileFields
} from 'stores/user-profile'

defineOptions({ name: 'PersonalizationSettingsPage' })

const $q = useQuasar()
const route = useRoute()
const router = useRouter()
const { confirm } = useConfirm()
const profile = useUserProfileStore()
const auth = useAuthStore()
const profileAudience = ref('private')
watch(() => auth.modelTrustEnabled, (enabled) => { if (!enabled) profileAudience.value = 'private' })
const addFieldDialogOpen = ref(false)
const activeFieldNames = ref([])
const draft = reactive(Object.fromEntries(
  userProfileFields.map((field) => [field, profileFactDraft()])
))
const savedDraft = reactive(Object.fromEntries(
  userProfileFields.map((field) => [field, profileFactDraft()])
))

const fields = [
  { name: 'fullName', icon: 'badge', label: 'Vollständiger Name', description: 'Dein vollständiger Name für persönlichen Kontext.' },
  { name: 'preferredName', icon: 'record_voice_over', label: 'Bevorzugter Name', description: 'Wie dich der Assistent nennen soll.' },
  { name: 'formOfAddress', icon: 'forum', label: 'Anrede', description: 'Zum Beispiel du, Sie oder eine andere gewünschte Anrede.' },
  { name: 'pronouns', icon: 'person', label: 'Pronomen', description: 'Zum Beispiel er/ihm, sie/ihr, they/them oder keine.' },
  { name: 'primaryLanguage', icon: 'translate', label: 'Bevorzugte Sprache', description: 'Die Sprache, in der du normalerweise angesprochen werden möchtest.' },
  { name: 'primaryResidence', icon: 'home', label: 'Hauptwohnsitz', description: 'Eine freiwillige Ortsangabe für regional passenden Kontext.' }
]
const audienceOptions = [
  { label: 'Private Modelle', value: 'private', icon: 'lock' },
  { label: 'Öffentliche Modelle', value: 'public', icon: 'public' }
]
const tab = computed({
  get: () => route.query.tab === 'assistant' ? 'assistant' : 'profile',
  set: (value) => router.replace({ query: { ...route.query, tab: value } })
})
const visibleFields = computed(() => fields.filter((field) => activeFieldNames.value.includes(field.name)))
const availableFields = computed(() => fields.filter((field) => !activeFieldNames.value.includes(field.name)))
const changes = computed(() => {
  const result = {}
  for (const field of userProfileFields) {
    if (
      draft[field].privateValue === savedDraft[field].privateValue &&
      draft[field].publicValue === savedDraft[field].publicValue &&
      draft[field].shared === savedDraft[field].shared
    ) continue
    result[field] = profileFactPayload(draft[field])
  }
  return result
})
const dirty = computed(() => Object.keys(changes.value).length > 0)

function assignDraft(value) {
  for (const field of userProfileFields) {
    const next = profileFactDraft(value[field])
    Object.assign(draft[field], next)
    Object.assign(savedDraft[field], next)
  }
  activeFieldNames.value = fields
    .filter((field) => Object.values(value[field.name] || {}).some((item) => item != null))
    .map((field) => field.name)
}

function addField(field) {
  if (!activeFieldNames.value.includes(field)) activeFieldNames.value.push(field)
  addFieldDialogOpen.value = false
}

async function removeField(field) {
  const current = draft[field.name]
  if ((current.privateValue != null || current.publicValue != null) && !await confirm(
    `Soll „${field.label}“ für private und öffentliche Modelle entfernt werden?`,
    'Angabe entfernen?',
    { okLabel: 'Entfernen' }
  )) return
  current.privateValue = null
  current.publicValue = null
  current.shared = false
  activeFieldNames.value = activeFieldNames.value.filter((name) => name !== field.name)
}

function fieldValue(field) {
  return profileAudience.value === 'private' || draft[field].shared
    ? draft[field].privateValue
    : draft[field].publicValue
}

function setFieldValue(field, value) {
  if (profileAudience.value === 'private') draft[field].privateValue = value
  else if (!draft[field].shared) draft[field].publicValue = value
}

function fieldDisabled(field) {
  return profile.loading || profile.saving || !profile.loaded || (
    profileAudience.value === 'public' && draft[field].shared
  )
}

function fieldHint(field) {
  return profileAudience.value === 'public' && draft[field.name].shared
    ? 'Wird aus „Private Modelle“ übernommen.'
    : field.description
}

async function load() {
  try {
    assignDraft(await profile.fetchProfile())
  } catch {
    // The central Axios interceptor displays the error message.
  }
}

async function save() {
  if (!profile.loaded || !dirty.value) return
  try {
    assignDraft(await profile.saveProfile(changes.value))
    $q.notify({ type: 'positive', message: 'Persönliches Profil gespeichert' })
  } catch {
    // The central Axios interceptor displays the error message.
  }
}

async function confirmDiscardedChanges() {
  if (profile.saving) return false
  if (!dirty.value) return true
  const accepted = await confirm(
    'Sollen die ungespeicherten Änderungen verworfen werden?',
    'Änderungen verwerfen?',
    { okLabel: 'Verwerfen' }
  )
  if (accepted) assignDraft(profile.snapshot())
  return accepted
}

onMounted(load)
onBeforeRouteUpdate(confirmDiscardedChanges)
onBeforeRouteLeave(confirmDiscardedChanges)
</script>
