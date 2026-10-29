<template>
  <section class="relative-position" :aria-busy="loading">
    <q-toolbar class="settings-detail-toolbar q-px-none q-mb-md">
      <q-btn flat round dense icon="arrow_back" aria-label="Zurück zu den Skills" :disable="busy" :to="{ name: listRoute }" />
      <div class="col q-px-sm">
        <div class="skill-name text-h6 text-weight-bold">{{ editingSkill ? skillDisplayName(editingSkill) : isCreate ? 'Skill erstellen' : 'Skill bearbeiten' }}</div>
        <div class="text-body2 app-muted">{{ admin ? 'Global – für alle Benutzer' : 'Persönlich' }} · SKILL.md und Begleitdateien</div>
      </div>
      <div class="settings-scope-actions row items-center q-gutter-sm">
        <q-btn v-if="editingSkill" flat no-caps no-wrap icon="download" label="Paket herunterladen" :disable="busy" @click="exportSkill" />
        <q-btn v-if="!readonly" unelevated no-caps no-wrap color="primary" icon="save" label="Speichern" :loading="saving" :disable="writeBlocked || binary || (!isCreate && !dirty)" @click="saveSkill" />
      </div>
    </q-toolbar>

    <q-banner v-if="loadFailed" rounded class="app-surface-tonal q-mb-md">
      {{ notFound ? 'Der Skill wurde nicht gefunden.' : 'Der Skill konnte nicht geladen werden.' }}
      <template #action><q-btn flat no-caps color="primary" label="Erneut laden" @click="load" /></template>
    </q-banner>
    <q-banner v-if="loaded && readonly" rounded class="app-surface-tonal q-mb-md">Dieser Skill kann nur angesehen und heruntergeladen werden.</q-banner>

    <q-card v-if="loaded" flat class="app-surface">
      <q-card-section>
        <div class="row items-center no-wrap">
          <q-select class="col q-mr-sm" dense outlined label="Datei" :transition-duration="0" :model-value="selectedPath" :options="editingSkill?.resourcePaths || ['SKILL.md']" :disable="busy" @update:model-value="selectFile" />
          <q-btn v-if="editingSkill" flat round icon="download" aria-label="Datei herunterladen" :disable="busy" @click="downloadResource" />
          <q-btn v-if="editingSkill && !readonly" flat round icon="add" aria-label="Datei hinzufügen" :disable="writeBlocked" @click="uploadOpen = true" />
          <q-btn v-if="editingSkill && selectedPath !== 'SKILL.md' && !readonly" flat round color="negative" icon="delete_outline" aria-label="Datei löschen" :disable="writeBlocked" @click="deleteResource" />
        </div>
        <q-checkbox v-if="selectedPath !== 'SKILL.md' && !binary" v-model="executable" :disable="readonly" label="Ausführbare Datei" />
      </q-card-section>
      <q-separator />
      <q-banner v-if="binary" rounded class="app-surface-tonal q-ma-md">Binärdatei – zum Anzeigen herunterladen oder durch eine neue Datei ersetzen.</q-banner>
      <WorkspaceFileEditor v-else v-model="document" class="skill-editor-content" :filename="selectedPath" :markdown="selectedPath.endsWith('.md')" :readonly="readonly" :dirty="dirty" :saving="busy" @save="saveSkill" @cancel="discardChanges" />
    </q-card>
    <q-inner-loading :showing="loading" />

    <q-dialog v-model="uploadOpen" :transition-duration="0" :persistent="skills.saving">
      <q-card class="q-pa-md">
        <div class="text-h6 q-mb-md">Paketdatei hinzufügen oder ersetzen</div>
        <q-input v-model="uploadPath" outlined label="Relativer Pfad" placeholder="scripts/example.py" class="q-mb-md" :disable="busy" />
        <q-file v-model="uploadFile" outlined label="Datei" :disable="busy" />
        <q-checkbox v-model="uploadExecutable" label="Ausführbar" :disable="busy" />
        <q-card-actions align="right">
          <q-btn flat no-caps label="Abbrechen" :disable="busy" v-close-popup />
          <q-btn color="primary" no-caps label="Speichern" :disable="writeBlocked || !uploadFile || !uploadPath.trim()" :loading="skills.saving" @click="uploadResource" />
        </q-card-actions>
      </q-card>
    </q-dialog>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { exportFile, useQuasar } from 'quasar'
import WorkspaceFileEditor from 'src/features/files/components/WorkspaceFileEditor.vue'
import { useConfirm } from 'src/composables/confirm'
import { skillDisplayName } from 'src/features/skills/skill-display'
import { useSkillStore } from 'stores/skills'
import { useAuthStore } from 'stores/auth'

defineOptions({ name: 'SkillEditorPage' })
const $q = useQuasar()
const route = useRoute()
const router = useRouter()
const { confirm } = useConfirm()
const skills = useSkillStore()
const auth = useAuthStore()
const admin = computed(() => String(route.name).startsWith('admin-'))
const scope = computed(() => admin.value ? 'instance' : 'personal')
const listRoute = computed(() => admin.value ? 'admin-skills' : 'settings-skills')
const editRoute = computed(() => admin.value ? 'admin-skill-edit' : 'settings-skill-edit')
const isCreate = computed(() => ['settings-skill-create', 'admin-skill-create'].includes(route.name))
const editingSkill = ref(null)
const loading = ref(false)
const loaded = ref(false)
const loadFailed = ref(false)
const notFound = ref(false)
const saving = ref(false)
const busy = computed(() => loading.value || saving.value || skills.saving)
const readonly = computed(() => admin.value ? !auth.isAdmin : !auth.isAdmin && !skills.personalSkillsEnabled)
const writeBlocked = computed(() => !loaded.value || !skills.settingsLoaded || readonly.value || busy.value)
const document = ref('')
const original = ref('')
const selectedPath = ref('SKILL.md')
const executable = ref(false)
const originalExecutable = ref(false)
const binary = ref(false)
const dirty = computed(() => document.value !== original.value || executable.value !== originalExecutable.value)
const uploadOpen = ref(false)
const uploadFile = ref(null)
const uploadPath = ref('')
const uploadExecutable = ref(false)
let loadToken = 0

async function load() {
  const token = ++loadToken
  loaded.value = false
  loading.value = true
  loadFailed.value = false
  notFound.value = false
  editingSkill.value = null
  skills.selectScope(scope.value)
  try {
    const [data] = await Promise.all([skills.fetchSkills(scope.value), skills.fetchSettings()])
    if (token !== loadToken) return
    if (!isCreate.value) {
      editingSkill.value = data.items.find((skill) => skill.id === route.params.skillId && skill.scope === scope.value)
      if (!editingSkill.value) { notFound.value = true; loadFailed.value = true; return }
    }
    document.value = editingSkill.value?.document ?? '---\nname: mein-skill\ndescription: Beschreibe, wann dieser Skill verwendet werden soll.\n---\n\n# Anweisungen\n\n'
    original.value = document.value
    selectedPath.value = 'SKILL.md'
    executable.value = false
    originalExecutable.value = false
    binary.value = false
    loaded.value = true
  } catch { if (token === loadToken) loadFailed.value = true } finally { if (token === loadToken) loading.value = false }
}

async function saveSkill() {
  if (writeBlocked.value || binary.value) return
  saving.value = true
  try {
    skills.selectScope(scope.value)
    const creating = !editingSkill.value
    const payload = { document: document.value, expectedRevision: editingSkill.value?.revision }
    const submittedExecutable = executable.value
    editingSkill.value = creating
      ? await skills.createSkill(payload)
      : selectedPath.value === 'SKILL.md'
        ? await skills.updateSkill(editingSkill.value.id, payload)
        : await skills.writeFile(editingSkill.value, selectedPath.value, new File([payload.document], selectedPath.value.split('/').pop()), submittedExecutable)
    original.value = payload.document
    originalExecutable.value = submittedExecutable
    if (creating) await router.replace({ name: editRoute.value, params: { skillId: editingSkill.value.id } })
    $q.notify({ type: 'positive', message: creating ? 'Skill erstellt' : 'Skill gespeichert' })
  } catch { /* Central API toast. */ } finally { saving.value = false }
}

async function readFile(path) {
  saving.value = true
  try {
    const response = await skills.readFile(editingSkill.value, path)
    let text = ''
    let isBinary = false
    try { text = new TextDecoder('utf-8', { fatal: true }).decode(response.data); isBinary = text.includes('\0') } catch { isBinary = true }
    document.value = text
    original.value = text
    selectedPath.value = path
    binary.value = isBinary
    executable.value = response.headers['x-skill-executable'] === 'true'
    originalExecutable.value = executable.value
    editingSkill.value = { ...editingSkill.value, revision: response.headers.etag.replaceAll('"', '') }
  } finally { saving.value = false }
}

async function selectFile(path) {
  if (busy.value || !editingSkill.value || path === selectedPath.value) return
  if (dirty.value && !await confirm('Ungespeicherte Änderungen verwerfen?', 'Datei wechseln')) return
  try { await readFile(path) } catch { /* Central API toast. */ }
}

async function exportSkill() {
  try { exportFile(`${editingSkill.value.name}.zip`, await skills.exportSkill(editingSkill.value), 'application/zip') } catch { /* Central API toast. */ }
}

async function downloadResource() {
  try { const { data } = await skills.readFile(editingSkill.value, selectedPath.value); exportFile(selectedPath.value.split('/').pop(), data) } catch { /* Central API toast. */ }
}

async function uploadResource() {
  if (writeBlocked.value) return
  if (editingSkill.value.resourcePaths.includes(uploadPath.value) && !await confirm('Die vorhandene Datei ersetzen?', 'Datei ersetzen')) return
  try {
    editingSkill.value = await skills.writeFile(editingSkill.value, uploadPath.value, uploadFile.value, uploadExecutable.value)
    if (uploadPath.value === selectedPath.value) await readFile(selectedPath.value)
    uploadOpen.value = false
    uploadFile.value = null
    $q.notify({ type: 'positive', message: 'Paketdatei gespeichert' })
  } catch { /* Central API toast. */ }
}

async function deleteResource() {
  if (writeBlocked.value || !await confirm(`Datei „${selectedPath.value}“ löschen?`, 'Datei löschen', { okColor: 'negative' })) return
  try {
    editingSkill.value = await skills.deleteFile(editingSkill.value, selectedPath.value)
    await readFile('SKILL.md')
    $q.notify({ type: 'positive', message: 'Paketdatei gelöscht' })
  } catch { /* Central API toast. */ }
}

async function discardChanges() {
  if (busy.value || !await confirm('Ungespeicherte Änderungen verwerfen?', 'Änderungen verwerfen', { okLabel: 'Verwerfen' })) return
  document.value = original.value
  executable.value = originalExecutable.value
}

function confirmLeave(to) {
  // Creating a skill only changes its URL; preserve edits made while saving.
  if (isCreate.value && to.name === editRoute.value && to.params.skillId === editingSkill.value?.id) return true
  if (busy.value) return false
  return !dirty.value || confirm('Ungespeicherte Änderungen verwerfen?', 'Seite verlassen', { okLabel: 'Verwerfen' })
}
function beforeUnload(event) {
  if (!dirty.value && !saving.value && !skills.saving) return
  event.preventDefault()
  event.returnValue = ''
}

onMounted(() => { load(); window.addEventListener('beforeunload', beforeUnload) })
onBeforeUnmount(() => { loadToken++; window.removeEventListener('beforeunload', beforeUnload) })
watch(() => [route.name, route.params.skillId], () => {
  if (route.name === editRoute.value && route.params.skillId === editingSkill.value?.id && editingSkill.value.scope === scope.value) return
  load()
})
onBeforeRouteUpdate(confirmLeave)
onBeforeRouteLeave(confirmLeave)
</script>

<style scoped>
.skill-name {
  overflow-wrap: anywhere;
}

.skill-editor-content {
  height: 65dvh;
  min-height: 20rem;
}
</style>
