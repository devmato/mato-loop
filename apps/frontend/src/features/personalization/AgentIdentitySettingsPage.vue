<template>
  <section>
    <header class="settings-section-header">
      <div>
        <h2 class="text-h6 text-weight-bold q-my-none">Personas</h2>
        <p class="text-body2 app-muted q-mb-none q-mt-xs">Wiederverwendbare Personas und abweichende Zuordnungen für Agenten verwalten.</p>
      </div>
      <q-btn v-if="showManagement" unelevated no-caps color="primary" icon="add" label="Persona erstellen" :disable="mutationBlocked" @click="openPersona()" />
    </header>
    <q-tabs v-if="admin" v-model="section" dense no-caps align="left" active-color="primary" indicator-color="primary" class="q-mb-lg">
      <q-tab name="general" icon="settings" label="Einstellungen" />
      <q-tab name="management" icon="face" label="Verwaltung" />
    </q-tabs>
    <q-banner v-if="identity.settingsLoadFailed" rounded class="bg-negative text-white q-mb-md">
      Die Personas konnten nicht geladen werden.
      <template #action><q-btn flat no-caps color="white" label="Erneut laden" @click="load" /></template>
    </q-banner>
    <div v-if="identity.settingsLoading" class="row justify-center q-pa-xl"><q-spinner color="primary" size="2.5rem" /></div>
    <template v-if="ready">
      <PersonalSettingsGate v-if="admin && section === 'general'" :model-value="identity.personalPersonasEnabled" title="Persönliche Personas erlauben" description="Benutzer können eigene Personas, persönliche Profilzuordnungen und ihren Agentennamen anpassen." :disabled="identity.settingsSaving" @update:model-value="setPersonalEnabled" />
      <template v-if="showManagement">
        <q-banner v-if="personalBlocked" rounded class="app-surface-tonal q-mb-md">Persönliche Anpassungen sind deaktiviert. Personas, persönlicher Standard, Zuordnungen und Agentenname bleiben gespeichert, werden aber nicht verwendet oder bearbeitet.</q-banner>
        <q-banner v-if="route.query.profileId && !identity.profiles.some((profile) => profile.id === route.query.profileId)" rounded class="app-surface-tonal q-mb-md">Der angefragte Agent ist in diesem Bereich nicht verfügbar.</q-banner>
        <q-card flat class="app-surface q-mb-md">
          <q-card-section>
            <div class="text-subtitle1 text-weight-bold">{{ admin ? 'Globaler Agentenname' : 'Mein Agentenname' }}</div>
            <div class="text-caption app-muted">Ein Anzeigename für alle Agenten{{ admin ? ' dieser Instanz' : ' in deinem Konto' }}.</div>
            <q-form @submit.prevent="saveDisplayName" class="q-mt-md">
              <q-input dense v-model="displayName" outlined label="Agentenname" maxlength="80" :placeholder="identity.effectiveDisplayName" :disable="mutationBlocked" :rules="admin ? [requiredRule] : []" />
              <div class="row justify-end q-gutter-sm q-mt-md">
                <q-btn v-if="!admin" flat no-caps label="Globalen Namen verwenden" :disable="mutationBlocked || !identity.configuredDisplayName" @click="resetDisplayName" />
                <q-btn unelevated no-caps color="primary" label="Speichern" type="submit" :loading="identity.settingsSaving" :disable="mutationBlocked || !nameDirty" />
              </div>
            </q-form>
          </q-card-section>
        </q-card>
        <q-card flat class="app-surface q-mb-md">
          <q-card-section>
            <div class="text-subtitle1 text-weight-bold">{{ admin ? 'Globale Personas' : 'Meine Personas' }}</div>
            <div class="text-caption app-muted">Eine Persona kann mehreren Agenten dienen. Änderungen gelten für alle Agenten, die sie verwenden.</div>
            <div v-if="!admin" class="text-caption app-muted q-mt-xs">Dein Standard gilt für alle Agenten ohne persönliche Zuordnung. Ohne persönlichen Standard gelten die globalen Vorgaben.</div>
            <AppSelect dense v-if="admin" class="q-mt-md" :model-value="identity.defaultPersonaId" outlined emit-value map-options label="Globale Standard-Persona" :options="personaOptions" :disable="mutationBlocked" @update:model-value="setDefaultPersona" />
          </q-card-section>
          <q-separator />
          <q-list separator>
            <q-item v-if="!managedPersonas.length"><q-item-section>Noch keine eigenen Personas angelegt.</q-item-section></q-item>
            <q-item v-for="persona in managedPersonas" :key="persona.id" class="q-py-md">
              <q-item-section>
                <q-item-label class="text-weight-medium">{{ persona.name }} <q-badge v-if="persona.id === identity.defaultPersonaId" color="primary" label="Standard" /><q-badge v-if="!admin && persona.id === identity.personalDefaultPersonaId" color="primary" label="Mein Standard" /></q-item-label>
                <q-item-label caption lines="2">{{ persona.instructions || 'Keine zusätzlichen Persona-Anweisungen.' }}</q-item-label>
              </q-item-section>
              <q-item-section side><div class="row no-wrap">
                <q-btn flat round dense :icon="personalBlocked ? 'visibility' : 'edit'" :aria-label="`${persona.name} ${personalBlocked ? 'ansehen' : 'bearbeiten'}`" :disable="identity.settingsSaving" @click="openPersona(persona)" />
                <q-btn flat round dense icon="delete_outline" color="negative" :aria-label="`${persona.name} löschen`" :disable="mutationBlocked || persona.id === identity.defaultPersonaId" @click="removePersona(persona)" />
              </div></q-item-section>
            </q-item>
          </q-list>
        </q-card>
        <q-card flat class="app-surface">
          <q-card-section class="row items-center q-col-gutter-md">
            <div class="col-12 col-sm">
              <div class="text-subtitle1 text-weight-bold">Abweichende Persona je Agent</div>
              <div class="text-caption app-muted">{{ admin ? 'Abweichende Persona für globale Agentenprofile auswählen.' : 'Globale oder eigene Persona für einen zugänglichen Agenten auswählen. Gilt nur für dich.' }}</div>
            </div>
            <q-btn class="col-12 col-sm-auto" flat no-caps color="primary" icon="add" label="Zuordnung hinzufügen" :disable="mutationBlocked || !availableProfiles.length" @click="openOverride()" />
          </q-card-section>
          <q-separator />
          <q-list separator>
            <q-item v-if="!Object.keys(identity.profilePersonas).length"><q-item-section>{{ admin ? 'Ohne Zuordnung gilt die globale Standard-Persona.' : 'Ohne persönliche Zuordnung gilt dein Standard, ansonsten die globale Vorgabe für den Agenten.' }}</q-item-section></q-item>
            <q-item v-for="(personaId, id) in identity.profilePersonas" :key="id" class="q-py-md">
              <q-item-section><q-item-label class="text-weight-medium">{{ profileName(id) }}</q-item-label><q-item-label caption>{{ personaName(personaId) }}</q-item-label></q-item-section>
              <q-item-section side><div class="row no-wrap">
                <q-btn flat round dense :icon="personalBlocked ? 'visibility' : 'edit'" :aria-label="`${profileName(id)}: Zuordnung öffnen`" :disable="identity.settingsSaving || !identity.profiles.some((profile) => profile.id === id)" @click="openOverride(id)" />
                <q-btn flat round dense icon="delete_outline" color="negative" :aria-label="`${profileName(id)}: Zuordnung löschen`" :disable="mutationBlocked" @click="removeOverride(id)" />
              </div></q-item-section>
            </q-item>
          </q-list>
        </q-card>
      </template>
    </template>
    <q-dialog v-model="dialogOpen" :transition-duration="0" :maximized="$q.screen.lt.sm" :persistent="identity.settingsSaving" aria-labelledby="persona-dialog-title">
      <q-card class="app-surface column no-wrap full-width">
        <q-form class="column no-wrap full-height" @submit.prevent="saveDialog">
          <q-card-section class="row items-center no-wrap">
            <div id="persona-dialog-title" class="text-h6 col">{{ assignmentDialog ? 'Persona zuordnen' : personalBlocked ? 'Persona ansehen' : editingPersonaId ? 'Persona bearbeiten' : 'Persona erstellen' }}</div>
            <q-btn flat round dense icon="close" aria-label="Dialog schließen" :disable="identity.settingsSaving" v-close-popup />
          </q-card-section>
          <q-separator />
          <q-card-section class="col scroll q-gutter-md">
            <q-banner v-if="personalBlocked" rounded class="app-surface-tonal">Persönliche Anpassungen sind deaktiviert.</q-banner>
            <template v-if="assignmentDialog">
              <AppSelect dense :model-value="profileId" outlined emit-value map-options label="Agentenprofil" :options="identity.profiles.filter((profile) => profile.id === profileId || !Object.hasOwn(identity.profilePersonas, profile.id)).map((profile) => ({ label: profile.name, value: profile.id }))" :disable="mutationBlocked" :rules="[requiredRule]" @update:model-value="selectProfile" />
              <div v-if="selectedProfile" class="text-caption app-muted">Vorgabe ohne Zuordnung: {{ personaName(selectedProfile.inheritedPersonaId) }}</div>
              <AppSelect dense v-model="selectedPersonaId" outlined emit-value map-options label="Persona" :options="personaOptions" :disable="mutationBlocked" :rules="[requiredRule]" />
              <q-input dense :model-value="selectedPersona?.instructions || ''" outlined type="textarea" rows="8" label="Persona-Anweisungen" readonly />
            </template>
            <template v-else>
              <q-input dense v-model="form.name" outlined label="Name" maxlength="80" :readonly="personalBlocked" :rules="[requiredRule]" />
              <q-input dense v-model="form.instructions" outlined type="textarea" rows="12" label="Verhalten und Ton" maxlength="32002" :readonly="personalBlocked" hint="Diese Anweisungen werden an das ausgewählte Modell übertragen." />
              <q-checkbox v-if="!admin" v-model="form.setAsDefault" label="Als meinen Standard verwenden" :disable="mutationBlocked" />
            </template>
          </q-card-section>
          <q-card-actions align="right">
            <q-btn flat no-caps label="Abbrechen" :disable="identity.settingsSaving" v-close-popup />
            <q-btn v-if="!personalBlocked" unelevated no-caps color="primary" label="Speichern" type="submit" :loading="identity.settingsSaving" :disable="mutationBlocked" />
          </q-card-actions>
        </q-form>
      </q-card>
    </q-dialog>
  </section>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, reactive, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from 'vue-router'
import { useConfirm } from 'src/composables/confirm'
import { useAgentIdentityStore } from 'stores/agent-identity'
import { useAuthStore } from 'stores/auth'
import PersonalSettingsGate from 'src/features/settings/PersonalSettingsGate.vue'

defineOptions({ name: 'AgentIdentitySettingsPage' })
const { admin } = defineProps({ admin: { type: Boolean, default: false } })
const $q = useQuasar()
const { confirm } = useConfirm()
const identity = useAgentIdentityStore()
const auth = useAuthStore()
const route = useRoute()
const section = ref('general')
const displayName = ref('')
const dialogOpen = ref(false)
const assignmentDialog = ref(false)
const editingPersonaId = ref(null)
const profileId = ref(null)
const selectedPersonaId = ref(null)
const form = reactive({ name: '', instructions: '', setAsDefault: false })
const scope = computed(() => admin ? 'instance' : 'personal')
const ready = computed(() => identity.settingsLoaded && !identity.settingsLoading && identity.scope === scope.value)
const showManagement = computed(() => !admin || section.value === 'management')
const personalBlocked = computed(() => !admin && !auth.isAdmin && !identity.personalPersonasEnabled)
const mutationBlocked = computed(() => !ready.value || identity.settingsSaving || personalBlocked.value)
const nameDirty = computed(() => displayName.value.trim() !== (identity.configuredDisplayName || ''))
const managedPersonas = computed(() => identity.personas.filter((persona) => persona.scope === scope.value))
const personaOptions = computed(() => identity.personas.map((persona) => ({ label: `${persona.name} (${persona.scope === 'instance' ? 'Global' : 'Persönlich'})`, value: persona.id })))
const availableProfiles = computed(() => identity.profiles.filter((profile) => !Object.hasOwn(identity.profilePersonas, profile.id)))
const selectedProfile = computed(() => identity.profiles.find((profile) => profile.id === profileId.value))
const selectedPersona = computed(() => identity.personas.find((persona) => persona.id === selectedPersonaId.value))
const profileName = (id) => identity.profiles.find((profile) => profile.id === id)?.name || 'Nicht mehr verfügbarer Agent'
const personaName = (id) => identity.personas.find((persona) => persona.id === id)?.name || 'Vorgabe (Persona nicht mehr verfügbar)'
const requiredRule = (value) => Boolean(String(value || '').trim()) || 'Pflichtfeld'

async function load() {
  dialogOpen.value = false
  try {
    await identity.fetchSettings(scope.value)
    displayName.value = identity.configuredDisplayName || ''
    if (ready.value && identity.profiles.some((profile) => profile.id === route.query.profileId)) {
      section.value = 'management'
      openOverride(route.query.profileId)
    }
  } catch { /* Central API toast. */ }
}
async function saveDisplayName() {
  if (!displayName.value.trim()) { if (!admin) await resetDisplayName(); return }
  try {
    await identity.saveDisplayName(displayName.value.trim())
    displayName.value = identity.configuredDisplayName || ''
    $q.notify({ type: 'positive', message: 'Agentenname gespeichert' })
  } catch { /* Central API toast. */ }
}
async function resetDisplayName() {
  try {
    await identity.resetDisplayName()
    displayName.value = ''
    $q.notify({ type: 'positive', message: 'Globaler Agentenname wird verwendet' })
  } catch { /* Central API toast. */ }
}
async function setPersonalEnabled(enabled) {
  try { await identity.setPersonalPersonasEnabled(enabled) } catch { /* Central API toast. */ }
}
async function setDefaultPersona(id) {
  try {
    await identity.setDefaultPersona(id)
    $q.notify({ type: 'positive', message: 'Standard-Persona gespeichert' })
  } catch { /* Central API toast. */ }
}
function openPersona(persona = null) {
  assignmentDialog.value = false
  editingPersonaId.value = persona?.id || null
  Object.assign(form, { name: persona?.name || '', instructions: persona?.instructions || '', setAsDefault: Boolean(persona && persona.id === identity.personalDefaultPersonaId) })
  dialogOpen.value = true
}
function selectProfile(id) {
  profileId.value = id
  const assigned = identity.profilePersonas[id]
  selectedPersonaId.value = identity.personas.some((persona) => persona.id === assigned) ? assigned : selectedProfile.value?.inheritedPersonaId || null
}
function openOverride(id = null) {
  assignmentDialog.value = true
  selectProfile(id)
  dialogOpen.value = true
}
async function saveDialog() {
  if (mutationBlocked.value) return
  try {
    if (assignmentDialog.value) await identity.savePersonaOverride(profileId.value, selectedPersonaId.value)
    else await identity.savePersona(editingPersonaId.value, {
      name: form.name.trim(), instructions: form.instructions.trim(),
      ...(!admin ? { setAsDefault: form.setAsDefault } : {})
    })
    dialogOpen.value = false
    $q.notify({ type: 'positive', message: assignmentDialog.value ? 'Persona-Zuordnung gespeichert' : 'Persona gespeichert' })
  } catch { /* Central API toast. */ }
}
async function removePersona(persona) {
  const defaultNotice = persona.id === identity.personalDefaultPersonaId ? ' Dein persönlicher Standard wird ebenfalls aufgehoben.' : ''
  if (!await confirm(`Persona „${persona.name}“ löschen? Ihre Zuordnungen fallen auf die Vorgabe zurück.${defaultNotice}`, 'Persona löschen?', { okLabel: 'Löschen', okColor: 'negative' })) return
  try { await identity.deletePersona(persona.id); $q.notify({ type: 'positive', message: 'Persona gelöscht' }) } catch { /* Central API toast. */ }
}
async function removeOverride(id) {
  if (!await confirm(`Persona-Zuordnung für „${profileName(id)}“ löschen? Danach gilt wieder die Vorgabe.`, 'Zuordnung löschen?', { okLabel: 'Löschen', okColor: 'negative' })) return
  try { await identity.deletePersonaOverride(id); $q.notify({ type: 'positive', message: 'Persona-Zuordnung gelöscht' }) } catch { /* Central API toast. */ }
}
const confirmDiscard = () => !identity.settingsSaving && (!nameDirty.value || confirm('Ungespeicherte Änderungen verwerfen?'))
watch(() => [admin, route.query.profileId], load, { immediate: true })
onBeforeRouteUpdate(confirmDiscard)
onBeforeRouteLeave(confirmDiscard)
</script>
