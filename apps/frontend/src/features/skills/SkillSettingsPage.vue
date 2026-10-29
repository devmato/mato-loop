<template>
  <router-view v-if="showEditor" />
  <section v-else>
    <header class="settings-section-header">
      <div>
        <h2 class="text-h6 text-weight-bold q-my-none">Skills</h2>
        <p class="text-body2 app-muted q-mb-none q-mt-xs">
          {{ admin ? 'Globale Skills für alle Benutzer verwalten.' : 'Eigene Anweisungen für spezialisierte Aufgaben verwalten.' }}
        </p>
      </div>
      <div v-if="showManagement" class="settings-scope-actions row items-center col-auto">
        <q-btn flat no-caps icon="upload_file" label="Importieren" :disable="writeBlocked" @click="importOpen = true" />
        <q-btn unelevated no-caps no-wrap color="primary" icon="add" label="Skill erstellen" :disable="writeBlocked" :to="{ name: admin ? 'admin-skill-create' : 'settings-skill-create' }" />
      </div>
    </header>

    <q-tabs v-if="admin" v-model="section" dense no-caps align="left" active-color="primary" indicator-color="primary" class="q-mb-lg">
      <q-tab name="general" icon="settings" label="Einstellungen" />
      <q-tab name="management" icon="school" label="Verwaltung" />
    </q-tabs>
    <PersonalSettingsGate v-if="admin && section === 'general'" :model-value="skills.personalSkillsEnabled" title="Persönliche Skills erlauben" description="Benutzer können eigene Skills und persönliche Anpassungen mitgelieferter Skills erstellen und verwenden." :disabled="writeBlocked" @update:model-value="setPersonalSkillsEnabled" />
    <q-banner v-if="loaded && personalBlocked" rounded class="app-surface-tonal q-mb-md">
      Persönliche Skills sind deaktiviert. Sie bleiben lesbar gespeichert, können aber nicht verändert werden und werden ab dem nächsten Agentenlauf nicht mehr verwendet. Globale Skills bleiben verfügbar.
    </q-banner>
    <q-banner v-if="loadFailed" rounded class="bg-negative text-white q-mb-md">
      <template #avatar><q-icon name="error_outline" /></template>
      Die Skills konnten nicht geladen werden.
      <template #action><q-btn flat no-caps color="white" label="Erneut laden" @click="load" /></template>
    </q-banner>

    <q-card v-if="showManagement" flat class="app-surface relative-position">
      <q-card-section v-if="skills.loading && !loaded" class="text-center q-pa-xl">
        <q-spinner color="primary" size="36px" />
        <div class="app-muted q-mt-sm">Skills werden geladen ...</div>
      </q-card-section>
      <q-card-section v-else-if="loaded && !skills.scopedItems.length" class="q-pa-xl">
        <AppStateMessage icon="school" title="Noch keine Skills" :description="admin ? 'Erstelle einen Skill für alle Benutzer.' : 'Erstelle einen persönlichen Skill.'" />
      </q-card-section>
      <q-list v-else separator>
        <q-item v-for="skill in skills.scopedItems" :key="skill.id" class="settings-list-item q-py-md">
          <q-item-section avatar top><q-avatar rounded color="primary" text-color="white" icon="school" /></q-item-section>
          <q-item-section>
            <q-item-label class="settings-list-name text-subtitle1 text-weight-medium">
              {{ skillDisplayName(skill) }}
              <q-badge v-if="skill.isDefault" outline color="primary" label="Standard" class="q-ml-xs" />
              <q-badge outline color="grey-7" :label="skill.scope === 'instance' ? 'Global' : 'Persönlich'" class="q-ml-xs" />
            </q-item-label>
            <q-item-label caption class="text-body2 q-mt-xs">{{ skillDisplayDescription(skill) }}</q-item-label>
          </q-item-section>
          <q-item-section side top class="settings-list-actions">
            <div class="row items-center justify-end no-wrap full-width q-gutter-sm">
              <q-toggle dense :model-value="skill.enabled" :aria-label="`${skillDisplayName(skill)} ${skill.enabled ? 'deaktivieren' : 'aktivieren'}`" :disable="writeBlocked || (skill.scope === 'instance' && !auth.isAdmin)" @update:model-value="setEnabled(skill, $event)" />
              <div class="row items-center no-wrap q-gutter-xs">
                <q-btn flat round dense :icon="personalBlocked ? 'visibility' : 'edit'" :aria-label="`${skillDisplayName(skill)} ${personalBlocked ? 'ansehen' : 'bearbeiten'}`" :to="{ name: admin ? 'admin-skill-edit' : 'settings-skill-edit', params: { skillId: skill.id } }" />
                <q-btn flat round dense icon="more_vert" :aria-label="`Weitere Aktionen für ${skillDisplayName(skill)}`" :disable="writeBlocked || (skill.scope === 'instance' && !auth.isAdmin) || (skill.isDefault && !(skill.modified && auth.isAdmin))">
                  <AppMenu>
                    <q-list>
                      <q-item v-if="skill.isDefault && skill.modified && auth.isAdmin" clickable v-close-popup @click="resetSkill(skill)">
                        <q-item-section avatar><q-icon name="restart_alt" /></q-item-section>
                        <q-item-section>Auf Standard zurücksetzen</q-item-section>
                      </q-item>
                      <q-item v-if="!skill.isDefault" clickable v-close-popup class="text-negative" @click="removeSkill(skill)">
                        <q-item-section avatar><q-icon name="delete_outline" /></q-item-section>
                        <q-item-section>Löschen</q-item-section>
                      </q-item>
                    </q-list>
                  </AppMenu>
                </q-btn>
              </div>
            </div>
          </q-item-section>
        </q-item>
      </q-list>
      <q-inner-loading :showing="skills.loading" />
    </q-card>

    <q-dialog v-model="importOpen" :transition-duration="0" :persistent="skills.saving">
      <q-card class="q-pa-md">
        <div class="text-h6">Skill importieren</div>
        <p>{{ admin ? 'Global – für alle Benutzer' : 'Persönlicher Skill' }}</p>
        <q-file v-model="importFile" outlined label="SKILL.md oder ZIP-Paket" accept=".md,.zip" :disable="skills.saving" />
        <q-card-actions align="right">
          <q-btn flat no-caps label="Abbrechen" :disable="skills.saving" v-close-popup />
          <q-btn color="primary" no-caps label="Importieren" :disable="!importFile" :loading="skills.saving" @click="importSkill" />
        </q-card-actions>
      </q-card>
    </q-dialog>
  </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useQuasar } from 'quasar'
import AppMenu from 'src/shared/ui/AppMenu.vue'
import AppStateMessage from 'src/shared/ui/AppStateMessage.vue'
import PersonalSettingsGate from 'src/features/settings/PersonalSettingsGate.vue'
import { useConfirm } from 'src/composables/confirm'
import { skillDisplayDescription, skillDisplayName } from 'src/features/skills/skill-display'
import { useSkillStore } from 'stores/skills'
import { useAuthStore } from 'stores/auth'

defineOptions({ name: 'SkillSettingsPage' })
const { admin } = defineProps({ admin: { type: Boolean, default: false } })
const $q = useQuasar()
const { confirm } = useConfirm()
const skills = useSkillStore()
const auth = useAuthStore()
const route = useRoute()
const showEditor = computed(() => ['settings-skill-create', 'settings-skill-edit', 'admin-skill-create', 'admin-skill-edit'].includes(route.name))
const loaded = ref(false)
const loadFailed = ref(false)
const scope = computed(() => admin ? 'instance' : 'personal')
const section = ref(showEditor.value ? 'management' : 'general')
const showManagement = computed(() => !admin || section.value === 'management')
const personalBlocked = computed(() => !admin && !auth.isAdmin && !skills.personalSkillsEnabled)
const writeBlocked = computed(() => !loaded.value || loadFailed.value || !skills.settingsLoaded || personalBlocked.value || skills.saving)
const importOpen = ref(false)
const importFile = ref(null)

async function load() {
  if (showEditor.value) return
  loaded.value = false
  loadFailed.value = false
  try {
    skills.selectScope(scope.value)
    await Promise.all([skills.fetchSkills(scope.value), skills.fetchSettings()])
    loaded.value = true
  } catch {
    loadFailed.value = true
  }
}

async function importSkill() {
  try {
    skills.selectScope(scope.value)
    await skills.importSkill(importFile.value)
    importOpen.value = false
    importFile.value = null
    $q.notify({ type: 'positive', message: 'Skill importiert' })
  } catch { /* Central API toast. */ }
}

async function removeSkill(skill) {
  if (!await confirm(`Soll der Skill „${skill.name}“ dauerhaft gelöscht werden?`, 'Skill löschen', { okLabel: 'Löschen', okColor: 'negative' })) return
  try {
    skills.selectScope(skill.scope)
    await skills.deleteSkill(skill.id)
    await load()
    $q.notify({ type: 'positive', message: 'Skill gelöscht' })
  } catch { /* Central API toast. */ }
}

async function setEnabled(skill, enabled) {
  try {
    skills.selectScope(skill.scope)
    await skills.setEnabled(skill.id, enabled)
    await load()
    $q.notify({ type: 'positive', message: enabled ? 'Skill aktiviert' : 'Skill deaktiviert' })
  } catch { /* Central API toast. */ }
}

async function setPersonalSkillsEnabled(enabled) {
  try {
    await skills.setPersonalSkillsEnabled(enabled)
    $q.notify({ type: 'positive', message: 'Einstellung gespeichert' })
  } catch { /* Central API toast. */ }
}

async function resetSkill(skill) {
  if (!await confirm(`Soll der Skill „${skillDisplayName(skill)}“ auf den ausgelieferten Standard zurückgesetzt werden?`, 'Skill zurücksetzen', { okLabel: 'Zurücksetzen' })) return
  try {
    skills.selectScope(skill.scope)
    await skills.resetSkill(skill.id)
    await load()
    $q.notify({ type: 'positive', message: 'Skill auf Standard zurückgesetzt' })
  } catch { /* Central API toast. */ }
}

onMounted(load)
watch(() => [admin, showEditor.value], load)
</script>
