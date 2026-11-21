<template>
  <router-view v-if="showEditor" />
  <section v-else>
    <header class="settings-section-header">
      <div>
        <h2 class="text-h6 text-weight-bold q-my-none">Agenten</h2>
        <p class="text-body2 app-muted q-mb-none q-mt-xs">
          {{ admin ? 'Globale Agentenprofile und den Fallback-Agenten verwalten.' : 'Eigene Agentenprofile mit Anweisungen und Arbeitsweise verwalten.' }}
        </p>
      </div>
      <div v-if="showManagement" class="settings-scope-actions row items-center col-auto">
        <q-btn
           v-if="!personalBlocked || admin"
          class="col-auto"
          unelevated
          no-caps
          no-wrap
          color="primary"
          icon="add"
          label="Neues Profil erstellen"
          :disable="agents.loading"
          :to="{ name: admin ? 'admin-agent-create' : 'settings-agent-create' }"
        />
      </div>
    </header>

    <q-tabs v-model="section" dense no-caps align="left" active-color="primary" indicator-color="primary" class="q-mb-lg">
      <q-tab v-if="admin" name="general" icon="settings" label="Einstellungen" />
      <q-tab name="management" icon="smart_toy" label="Verwaltung" />
      <q-tab name="integrations" icon="extension" label="Integrationsübersicht" />
    </q-tabs>

    <PersonalSettingsGate v-if="admin && section === 'general'" :model-value="agents.personalAgentsEnabled" title="Persönliche Agentenprofile erlauben" description="Benutzer können eigene Agentenprofile erstellen und verwenden." :disabled="gateSaving || agents.loading || agents.loadFailed" @update:model-value="setPersonalAgentsEnabled" />

    <AppSelect dense v-if="admin && showManagement" :model-value="agents.defaultAgentProfileId" outlined emit-value map-options label="Fallback-Agent" hint="Für Funktionen ohne explizite Agentenauswahl. Neue Chats verwenden den Startagenten ihrer Sessionvorlage." class="q-mb-md" :options="visibleAgents.filter((agent) => agent.enabled !== false).map((agent) => ({ label: agent.name, value: agent.id }))" :disable="agents.loading || gateSaving" @update:model-value="setDefaultProfile" />

    <q-banner v-if="personalBlocked" rounded class="app-surface-tonal q-mb-md">
      <template #avatar><q-icon name="person" color="primary" /></template>
      Persönliche Agenten wurden für diese Instanz deaktiviert.
    </q-banner>

    <q-banner v-if="agents.loadFailed" rounded class="bg-negative text-white q-mb-md">
      <template #avatar><q-icon name="error_outline" /></template>
      Die Agentenprofile konnten nicht geladen werden.
      <template #action><q-btn flat no-caps color="white" label="Erneut laden" @click="load" /></template>
    </q-banner>

    <q-card v-if="showManagement" flat class="app-surface relative-position">
      <q-card-section v-if="agents.loaded && !visibleAgents.length" class="q-pa-xl">
        <AppStateMessage
          icon="smart_toy"
          title="Noch keine Agentenprofile"
          description="Erstelle ein Profil mit eigenen Anweisungen und Berechtigungen."
        />
      </q-card-section>
      <q-list v-else separator>
        <q-item v-for="agent in visibleAgents" :key="agent.id" class="settings-list-item q-py-md">
          <q-item-section avatar top>
            <q-avatar rounded color="primary" text-color="white" icon="smart_toy" />
          </q-item-section>
          <q-item-section>
            <q-item-label class="settings-list-name text-subtitle1 text-weight-medium">
              {{ agent.name }} <q-badge outline color="grey-7" :label="agent.scope === 'instance' ? 'Global' : 'Persönlich'" />
              <q-badge v-if="agent.enabled === false" outline color="grey-7" label="Deaktiviert" />
              <q-badge v-if="agent.effective === false" outline color="warning" label="Nicht verfügbar" />
            </q-item-label>
            <q-item-label caption class="text-body2 q-mt-xs">
              {{ agent.description || 'Keine Beschreibung' }}
            </q-item-label>
            <q-item-label caption class="q-mt-xs">
              Persona: {{ agent.resolvedPersonaName || 'Standard' }}
            </q-item-label>
            <q-item-label caption class="q-mt-xs">
              Integrationen: {{ activeIntegrationCount(agent.id) }} aktiv
            </q-item-label>
          </q-item-section>
          <q-item-section side top class="settings-list-actions">
            <div class="row items-center no-wrap q-gutter-sm">
              <q-toggle
                dense
                :model-value="agent.enabled !== false"
                :aria-label="`${agent.name} ${agent.enabled === false ? 'aktivieren' : 'deaktivieren'}`"
                :disable="agent.id === agents.defaultAgentProfileId || switchingId === agent.id || agents.saving || personalBlocked"
                @update:model-value="setAgentEnabled(agent, $event)"
              />
              <div class="row items-center no-wrap q-gutter-xs">
                <q-btn
                  flat
                  round
                  dense
                  :icon="agent.scope === 'instance' && !auth.isAdmin ? 'visibility' : 'edit'"
                  :aria-label="`${agent.name} ${agent.scope === 'instance' && !auth.isAdmin ? 'ansehen' : 'bearbeiten'}`"
                  :disable="agent.scope === 'personal' && personalBlocked"
                  :to="{ name: admin ? 'admin-agent-edit' : 'settings-agent-edit', params: { agentId: agent.id } }"
                ><q-tooltip>{{ agent.scope === 'instance' && !auth.isAdmin ? 'Ansehen' : 'Bearbeiten' }}</q-tooltip></q-btn>
                <q-btn flat round dense icon="more_vert" :aria-label="`Weitere Aktionen für ${agent.name}`" :disable="!(agent.scope === 'personal' || auth.isAdmin) || personalBlocked || agent.protected || agent.id === agents.defaultAgentProfileId">
                  <AppMenu>
                    <q-list>
                      <q-item v-if="agent.scope === 'personal' || auth.isAdmin" clickable v-close-popup class="text-negative" :disable="personalBlocked || agent.protected || agent.id === agents.defaultAgentProfileId" @click="removeAgent(agent)">
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
      <q-inner-loading :showing="agents.loading" />
    </q-card>
    <AgentIntegrationOverview v-else-if="showIntegrationOverview" :admin="admin" />
  </section>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import AppMenu from 'src/shared/ui/AppMenu.vue'
import { computed, onMounted, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useRoute } from 'vue-router'
import AppStateMessage from 'src/shared/ui/AppStateMessage.vue'
import { useConfirm } from 'src/composables/confirm'
import { useAgentProfileStore } from 'stores/agent-profiles'
import { useAuthStore } from 'stores/auth'
import PersonalSettingsGate from 'src/features/settings/PersonalSettingsGate.vue'
import AgentIntegrationOverview from './AgentIntegrationOverview.vue'

defineOptions({ name: 'AgentProfileSettingsPage' })
const { admin } = defineProps({ admin: { type: Boolean, default: false } })

const $q = useQuasar()
const route = useRoute()
const { confirm } = useConfirm()
const agents = useAgentProfileStore()
const auth = useAuthStore()
const showEditor = computed(() => ['settings-agent-create', 'settings-agent-edit', 'admin-agent-create', 'admin-agent-edit'].includes(route.name))
const switchingId = ref(null)
const gateSaving = ref(false)
const section = ref(admin ? 'general' : 'management')
const showManagement = computed(() => section.value === 'management')
const showIntegrationOverview = computed(() => section.value === 'integrations')
const visibleAgents = computed(() => agents.manageableAgents.filter((agent) => agent.scope === (admin ? 'instance' : 'personal')))
const personalBlocked = computed(() => !admin && !auth.isAdmin && !agents.personalAgentsEnabled)

function activeIntegrationCount(profileId) {
  return agents.integrations.filter((integration) => (
    integration.profiles?.some((profile) => profile.id === profileId && profile.effective)
  )).length
}

async function load() {
  try {
    agents.selectScope(admin ? 'instance' : 'personal')
    await agents.fetchAgents(admin ? 'instance' : 'personal')
  } catch {
    agents.loadFailed = true
  }
}

async function removeAgent(agent) {
  const accepted = await confirm(
    `Soll der Agent „${agent.name}“ dauerhaft gelöscht werden? Verwendete Sessions wechseln zum aktuellen Standardprofil.`,
    'Agent löschen',
    { okLabel: 'Löschen', okColor: 'negative' }
  )
  if (!accepted) return
  try {
    agents.selectScope(agent.scope)
    await agents.deleteAgent(agent.id)
    await load()
    $q.notify({ type: 'positive', message: 'Agent gelöscht' })
  } catch {
    // The central Axios interceptor displays the error.
  }
}

async function setPersonalAgentsEnabled(enabled) {
  gateSaving.value = true
  try {
    await agents.setPersonalAgentsEnabled(enabled)
    await load()
    $q.notify({ type: 'positive', message: 'Einstellung gespeichert' })
  } catch {
    // The central Axios interceptor displays the error.
  } finally {
    gateSaving.value = false
  }
}

async function setAgentEnabled(agent, enabled) {
  switchingId.value = agent.id
  try {
    await agents.setAgentEnabled(agent.id, enabled, agent.scope)
    await load()
    $q.notify({ type: 'positive', message: enabled ? 'Agent aktiviert' : 'Agent deaktiviert' })
  } catch {
    // The central Axios interceptor displays the error.
  } finally {
    switchingId.value = null
  }
}

async function setDefaultProfile(id) {
  gateSaving.value = true
  try {
    await agents.setDefaultAgent(id)
    $q.notify({ type: 'positive', message: 'Standardprofil gespeichert' })
  } catch { /* Central API toast. */ }
  finally { gateSaving.value = false }
}

onMounted(() => {
  if (!showEditor.value) void load()
})
watch(showEditor, (editing) => {
  if (!editing) void load()
})
watch(() => admin, load)
</script>
