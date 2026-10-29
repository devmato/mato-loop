<template>
  <section>
    <header class="settings-section-header">
      <div>
        <h2 class="text-h6 text-weight-bold q-my-none">Grundeinstellungen</h2>
        <p class="text-body2 app-muted q-mb-none q-mt-xs">Persönliche Vorgaben für sprachabhängige Funktionen.</p>
      </div>
      <q-btn color="primary" unelevated no-caps icon="save" label="Speichern" :loading="settings.saving" :disable="settings.loading || !settings.loaded || settings.loadFailed || !dirty" @click="save" />
    </header>
    <q-banner v-if="settings.loadFailed" rounded class="bg-negative text-white q-mb-md">
      Die Grundeinstellungen konnten nicht geladen werden.
      <template #action><q-btn flat no-caps color="white" label="Erneut laden" @click="load" /></template>
    </q-banner>
    <q-card flat class="app-surface">
      <q-card-section>
        <AppSelect v-model="language" outlined emit-value map-options use-input label="Bevorzugte Sprache" :options="languageOptions" :disable="settings.loading || settings.saving || !settings.loaded || settings.loadFailed" @new-value="addLanguage" />
        <p class="text-body2 app-muted q-mb-none q-mt-md">Vorgabe für Transkriptionen und die Browser-Sprache von Jina Reader. Eine ausdrücklich angegebene Sprache hat Vorrang. „Automatisch“ verwendet die Erkennung oder Vorgabe des Dienstes. Keine Übersetzung oder Änderung der Oberflächensprache.</p>
        <p class="text-caption app-muted q-mb-none">Weitere Sprachen können als BCP-47-Kürzel eingegeben werden, zum Beispiel de-CH. Die Unterstützung hängt vom Dienst ab.</p>
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
import { useUserPreferencesStore } from './user-preferences-store'

const settings = useUserPreferencesStore()
const $q = useQuasar()
const { confirm } = useConfirm()
const language = ref(null)
const dirty = computed(() => settings.loaded && language.value !== settings.preferredLanguage)
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
  try { language.value = (await settings.fetchSettings()).preferredLanguage }
  catch { /* Central API toast. */ }
}
async function save() {
  if (settings.loading || settings.saving || !settings.loaded || settings.loadFailed || !dirty.value) return
  try {
    language.value = (await settings.saveSettings(language.value)).preferredLanguage
    $q.notify({ type: 'positive', message: 'Grundeinstellungen gespeichert.' })
  } catch { /* Central API toast. */ }
}
onMounted(load)
onBeforeRouteLeave(() => !settings.saving && (!dirty.value || confirm('Sollen die ungespeicherten Änderungen verworfen werden?', 'Änderungen verwerfen?', { okLabel: 'Verwerfen' })))
</script>
