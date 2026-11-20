<template>
  <section>
    <header class="settings-section-header">
      <div>
        <h2 class="text-h6 text-weight-bold q-my-none">Grundeinstellungen</h2>
        <p class="text-body2 app-muted q-mb-none q-mt-xs">Persönliche Vorgaben für Sprache, Navigation und Papierkorb.</p>
      </div>
      <q-btn color="primary" unelevated no-caps icon="save" label="Speichern" :loading="settings.saving" :disable="settings.loading || !settings.loaded || settings.loadFailed || !dirty || !validRetention || !validSessionsPageSize || !validSessionTabsLimit" @click="save" />
    </header>
    <q-banner v-if="settings.loadFailed" rounded class="bg-negative text-white q-mb-md">
      Die Grundeinstellungen konnten nicht geladen werden.
      <template #action><q-btn flat no-caps color="white" label="Erneut laden" @click="load" /></template>
    </q-banner>
    <q-card flat class="app-surface">
      <q-card-section>
        <AppSelect v-model="language" outlined emit-value map-options use-input label="Bevorzugte Sprache" :options="languageOptions" :disable="settings.loading || settings.saving || !settings.loaded || settings.loadFailed" @new-value="addLanguage" />
        <p class="text-body2 app-muted q-mb-none q-mt-md">Vorgabe für Chatantworten, Transkriptionen und die Browser-Sprache von Jina Reader. Eine ausdrücklich angegebene Sprache hat Vorrang. Bei „Automatisch“ folgt der Chat deinem persönlichen Sprachprofil, sonst deiner Anfrage; Dienste verwenden ihre Erkennung oder Vorgabe. Keine Übersetzung abgerufener Inhalte oder Änderung der Oberflächensprache.</p>
        <p class="text-caption app-muted q-mb-none">Weitere Sprachen können als BCP-47-Kürzel eingegeben werden, zum Beispiel de-CH. Die Unterstützung hängt vom Dienst ab.</p>
      </q-card-section>
      <q-separator />
      <q-card-section>
        <q-toggle v-model="trashEnabled" label="Papierkorb verwenden" :disable="settings.loading || settings.saving || !settings.loaded || settings.loadFailed" />
        <q-input v-model.number="trashRetentionDays" class="q-mt-md" outlined type="number" min="1" max="3650" step="1" label="Aufbewahrung in Tagen" :rules="[() => validRetention || 'Bitte eine ganze Zahl von 1 bis 3650 eingeben.']" :disable="settings.loading || settings.saving || !settings.loaded || settings.loadFailed" />
        <p class="text-body2 app-muted q-mb-none">Standardmäßig ausgeschaltet; Vorgabe für die Aufbewahrung: 30 Tage. Änderungen gelten nur für künftige Löschvorgänge. Deaktivieren leert den Papierkorb nicht; vorhandene Einträge bleiben über die Seitenleiste bis zu ihrer festen Frist wiederherstellbar.</p>
      </q-card-section>
    </q-card>
    <q-card flat class="app-surface q-mt-md">
      <q-card-section>
        <AppSelect v-model="sessionsPosition" outlined emit-value map-options label="Position der Sessions" :options="sessionsPositionOptions" :disable="settings.loading || settings.saving || !settings.loaded || settings.loadFailed" />
        <p class="text-body2 app-muted q-mb-none q-mt-md">Zeige den Bereich Sessions über oder unter den Projekten in der Seitenleiste. Sessions innerhalb eines Projekts bleiben beim jeweiligen Projekt.</p>
      </q-card-section>
      <q-separator />
      <q-card-section>
        <q-input v-model.number="sessionsPageSize" outlined type="number" min="1" max="100" step="1" label="Sessions pro Ladeschritt" :rules="[() => validSessionsPageSize || 'Bitte eine ganze Zahl von 1 bis 100 eingeben.']" :disable="settings.loading || settings.saving || !settings.loaded || settings.loadFailed" />
        <p class="text-body2 app-muted q-mb-none">Anzahl beim ersten Laden und bei „Mehr anzeigen“ in der Seitenleiste, auch innerhalb von Projekten. Standard: 8. Änderungen gelten ab dem nächsten Laden; bereits geladene Sessions bleiben sichtbar.</p>
      </q-card-section>
      <q-separator />
      <q-card-section>
        <q-toggle v-model="sessionTabsEnabled" label="Session-Tab-Leiste anzeigen" :disable="settings.loading || settings.saving || !settings.loaded || settings.loadFailed" />
        <AppSelect v-model="sessionTabsPosition" class="q-mt-md" outlined emit-value map-options label="Position der Session-Tab-Leiste" :options="[{ label: 'Oben', value: 'top' }, { label: 'Unten', value: 'bottom' }]" :disable="settings.loading || settings.saving || !settings.loaded || settings.loadFailed || !sessionTabsEnabled" />
        <AppSelect v-model="sessionTabsOrder" class="q-mt-md" outlined emit-value map-options label="Session-Tabs sortieren" :options="[{ label: 'Nach Projekt', value: 'project' }, { label: 'Nach Hinzufügen', value: 'added' }]" :disable="settings.loading || settings.saving || !settings.loaded || settings.loadFailed || !sessionTabsEnabled" />
        <p class="text-body2 app-muted q-mb-none q-mt-sm">Optional auf Desktop und Tablet, auf dem Handy ausgeblendet. Ein Klick öffnet eine Vorschau; Doppelklick, „In neuem Tab öffnen“ oder die erste Eingabe behalten den Tab. Das Schließen eines Tabs löscht weder Session noch Entwurf.</p>
        <q-input v-model.number="sessionTabsLimit" class="q-mt-md" outlined type="number" min="1" :max="MAX_SESSION_TABS_LIMIT" step="1" label="Maximal offene Session-Tabs" :rules="[() => validSessionTabsLimit || `Bitte eine ganze Zahl von 1 bis ${MAX_SESSION_TABS_LIMIT} eingeben.`]" :disable="settings.loading || settings.saving || !settings.loaded || settings.loadFailed || !sessionTabsEnabled" />
        <p class="text-body2 app-muted q-mb-none">Standard: {{ DEFAULT_SESSION_TABS_LIMIT }}. Beim Öffnen eines neuen Tabs machen die am längsten nicht genutzten Tabs Platz. Laufende Sessions und Entwürfe bleiben erhalten.</p>
      </q-card-section>
    </q-card>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useQuasar } from 'quasar'
import { onBeforeRouteLeave } from 'vue-router'
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { useConfirm } from 'src/composables/confirm'
import { DEFAULT_SESSION_TABS_LIMIT, MAX_SESSION_TABS_LIMIT, useUserPreferencesStore } from './user-preferences-store'

const settings = useUserPreferencesStore()
const $q = useQuasar()
const { confirm } = useConfirm()
const language = ref(null)
const sessionsPosition = ref('belowProjects')
const sessionsPageSize = ref(8)
const sessionTabsEnabled = ref(false)
const sessionTabsPosition = ref('bottom')
const sessionTabsOrder = ref('project')
const sessionTabsLimit = ref(DEFAULT_SESSION_TABS_LIMIT)
const validSessionTabsLimit = computed(() => Number.isInteger(sessionTabsLimit.value) && sessionTabsLimit.value >= 1 && sessionTabsLimit.value <= MAX_SESSION_TABS_LIMIT)
const sessionsPositionOptions = [
  { label: 'Unter den Projekten', value: 'belowProjects' },
  { label: 'Über den Projekten', value: 'aboveProjects' }
]
const trashEnabled = ref(false)
const trashRetentionDays = ref(30)
const validRetention = computed(() => Number.isInteger(trashRetentionDays.value) && trashRetentionDays.value >= 1 && trashRetentionDays.value <= 3650)
const validSessionsPageSize = computed(() => Number.isInteger(sessionsPageSize.value) && sessionsPageSize.value >= 1 && sessionsPageSize.value <= 100)
const dirty = computed(() => settings.loaded && (language.value !== settings.preferredLanguage || sessionsPosition.value !== settings.sessionsPosition || sessionsPageSize.value !== settings.sessionsPageSize || sessionTabsEnabled.value !== settings.sessionTabsEnabled || sessionTabsPosition.value !== settings.sessionTabsPosition || sessionTabsOrder.value !== settings.sessionTabsOrder || sessionTabsLimit.value !== settings.sessionTabsLimit || trashEnabled.value !== settings.trashEnabled || trashRetentionDays.value !== settings.trashRetentionDays))
const languagePattern = /^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$/
const languageOptions = computed(() => {
  const options = [{ label: 'Automatisch', value: null }, { label: 'Deutsch', value: 'de-DE' }, { label: 'Englisch', value: 'en-US' }]
  if (language.value && !options.some(option => option.value === language.value)) options.push({ label: language.value, value: language.value })
  return options
})
function addLanguage(value, done) {
  value = value.trim()
  if (value.length <= 35 && languagePattern.test(value)) done(value, 'add-unique')
  else {
    done()
    $q.notify({ type: 'negative', message: 'Bitte ein gültiges Sprachkürzel eingeben, zum Beispiel de-DE.' })
  }
}
async function load() {
  try { await settings.fetchSettings(); reset() }
  catch { /* Central API toast. */ }
}
async function save() {
  if (settings.loading || settings.saving || !settings.loaded || settings.loadFailed || !dirty.value || !validRetention.value || !validSessionsPageSize.value || !validSessionTabsLimit.value) return
  try {
    await settings.saveSettings({ preferredLanguage: language.value, sessionsPosition: sessionsPosition.value, sessionsPageSize: sessionsPageSize.value, sessionTabsEnabled: sessionTabsEnabled.value, sessionTabsPosition: sessionTabsPosition.value, sessionTabsOrder: sessionTabsOrder.value, sessionTabsLimit: sessionTabsLimit.value, trashEnabled: trashEnabled.value, trashRetentionDays: trashRetentionDays.value })
    reset()
    $q.notify({ type: 'positive', message: 'Grundeinstellungen gespeichert.' })
  } catch { /* Central API toast. */ }
}
function reset() {
  language.value = settings.preferredLanguage
  sessionsPosition.value = settings.sessionsPosition
  sessionsPageSize.value = settings.sessionsPageSize
  sessionTabsEnabled.value = settings.sessionTabsEnabled
  sessionTabsPosition.value = settings.sessionTabsPosition
  sessionTabsOrder.value = settings.sessionTabsOrder
  sessionTabsLimit.value = settings.sessionTabsLimit
  trashEnabled.value = settings.trashEnabled
  trashRetentionDays.value = settings.trashRetentionDays
}
onMounted(load)
onBeforeRouteLeave(() => !settings.saving && (!dirty.value || confirm('Sollen die ungespeicherten Änderungen verworfen werden?', 'Änderungen verwerfen?', { okLabel: 'Verwerfen' })))
</script>
